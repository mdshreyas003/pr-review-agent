"""Azure DevOps REST client: Repos for the diff, Boards for the story.

Everything the agent knows about the outside world comes through here. Auth is
either a PAT sent as HTTP Basic with an empty username (`ADO_AUTH_MODE=pat`),
or an Entra ID service principal authenticating with a client certificate
(`ADO_AUTH_MODE=service_principal`), which trades a long-lived PAT for a
short-lived bearer token. All calls run behind the shared circuit breaker so a
bad ADO minute degrades the review instead of hanging five agents.

`get_ado_client`/`set_ado_client_factory` are the single seam that constructs a
client. Two code paths reach Azure DevOps - the orchestrator when it posts a
review, and the HITL endpoint when a human approves one - and both must be
swappable for tests. Keeping the seam here is what prevents a second caller
quietly bypassing it.
"""

from __future__ import annotations

import base64
import re
import time
from collections.abc import Callable
from typing import Any

import httpx
import structlog

from app.config import settings
from app.contracts.models import (
    DiffHunk,
    FileDiff,
    PullRequestRef,
    StoryContext,
    WorkItem,
)
from app.resilience import with_resilience

log = structlog.get_logger(__name__)

_BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".dll", ".exe", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".jar", ".class", ".pyc", ".bin", ".lock",
}

_CHANGE_TYPE_MAP = {
    "add": "add",
    "edit": "edit",
    "delete": "delete",
    "rename": "rename",
    "sourceRename": "rename",
    "edit, rename": "rename",
}


class AzureDevOpsError(RuntimeError):
    pass


# Azure DevOps' well-known first-party app id in Entra ID - every service
# principal's token is requested against this resource, never against the
# organization or a caller-specific app registration.
_ADO_RESOURCE_SCOPE = "499b84ac-1321-427f-aa17-267ca6975798/.default"

# Refresh the token a little before it actually expires, so a long-running
# review cannot straddle the boundary mid-request.
_TOKEN_SKEW_SECONDS = 300


def short_ref(ref: str) -> str:
    return ref.replace("refs/heads/", "") if ref else ""


def pull_request_ref_from_resource(
    resource: dict[str, Any],
    *,
    default_org_url: str = "",
    default_project: str = "",
) -> PullRequestRef:
    """Build a PullRequestRef from either a service-hook body or the REST API.

    Both carry the same shape, and both triggers must produce byte-identical
    refs: the idempotency key is derived from this, so any divergence between
    the webhook path and the polling path would review the same commit twice.
    """
    repository = resource.get("repository") or {}
    project = repository.get("project") or {}
    created_by = resource.get("createdBy") or {}

    org_url = default_org_url or settings.ado_org_url
    repo_url = repository.get("url") or ""
    if repo_url and "/_apis/" in repo_url:
        # https://dev.azure.com/org/{projectId}/_apis/git/repositories/{id}
        org_url = repo_url.split("/_apis/")[0].rsplit("/", 1)[0]

    return PullRequestRef(
        organization_url=org_url.rstrip("/"),
        project=project.get("name") or default_project or settings.ado_project,
        repository_id=str(repository.get("id") or ""),
        repository_name=repository.get("name", ""),
        pull_request_id=int(resource.get("pullRequestId") or 0),
        title=resource.get("title", "") or "",
        description=resource.get("description", "") or "",
        source_branch=short_ref(resource.get("sourceRefName", "")),
        target_branch=short_ref(resource.get("targetRefName", "")),
        author=created_by.get("displayName") or created_by.get("uniqueName", ""),
        source_commit=(resource.get("lastMergeSourceCommit") or {}).get("commitId", ""),
        target_commit=(resource.get("lastMergeTargetCommit") or {}).get("commitId", ""),
        is_draft=bool(resource.get("isDraft", False)),
    )


def idempotency_key_for(pr: PullRequestRef, fallback: str = "") -> str:
    """Keyed on the head commit, so a re-delivery collapses but a push does not.

    `fallback` is the webhook notification id, used only when the payload has
    no merge commit yet.
    """
    return f"{pr.repository_id}:{pr.pull_request_id}:{pr.source_commit or fallback}"


class AzureDevOpsClient:
    def __init__(
        self,
        org_url: str | None = None,
        pat: str | None = None,
        client: httpx.AsyncClient | None = None,
        auth_mode: str | None = None,
    ) -> None:
        self.org_url = (org_url or settings.ado_org_url).rstrip("/")
        self.api_version = settings.ado_api_version
        self.auth_mode = auth_mode or settings.ado_auth_mode
        self._owns_client = client is None
        self._http = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._credential: Any = None
        self._token: str = ""
        self._token_expires_at: float = 0.0

        if self.auth_mode == "service_principal":
            self._base_headers = {"Accept": "application/json"}
        else:
            pat = pat if pat is not None else settings.ado_pat
            token = base64.b64encode(f":{pat}".encode()).decode()
            self._base_headers = {
                "Authorization": f"Basic {token}",
                "Accept": "application/json",
            }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()
        if self._credential is not None:
            await self._credential.close()

    async def __aenter__(self) -> AzureDevOpsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------------- auth
    async def _auth_headers(self) -> dict[str, str]:
        if self.auth_mode != "service_principal":
            return self._base_headers

        now = time.time()
        if not self._token or now >= self._token_expires_at - _TOKEN_SKEW_SECONDS:
            self._token, self._token_expires_at = await self._fetch_token()
        return {**self._base_headers, "Authorization": f"Bearer {self._token}"}

    async def _fetch_token(self) -> tuple[str, float]:
        try:
            from azure.identity.aio import CertificateCredential
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise AzureDevOpsError(
                "Service-principal auth needs the azure-identity package "
                "(pip install azure-identity)"
            ) from exc

        if self._credential is None:
            self._credential = CertificateCredential(
                tenant_id=settings.ado_tenant_id,
                client_id=settings.ado_client_id,
                certificate_path=settings.ado_client_certificate_path,
                password=settings.ado_client_certificate_password or None,
            )
        try:
            token = await self._credential.get_token(_ADO_RESOURCE_SCOPE)
        except Exception as exc:  # noqa: BLE001
            raise AzureDevOpsError(
                f"Could not obtain an Entra token for Azure DevOps ({_ADO_RESOURCE_SCOPE}). "
                "Check the service principal is added as a user in the ADO "
                "organization (Organization Settings -> Users) with the right "
                "permissions, and that ADO_TENANT_ID/ADO_CLIENT_ID/"
                f"ADO_CLIENT_CERTIFICATE_PATH are correct. ({exc})"
            ) from exc
        log.info("ado.token.acquired", expires_in=int(token.expires_on - time.time()))
        return token.token, float(token.expires_on)

    # ----------------------------------------------------------------- plumbing
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        api_version: str | None = None,
        raw_text: bool = False,
    ) -> Any:
        url = f"{self.org_url}/{path.lstrip('/')}"
        query = {"api-version": api_version or self.api_version, **(params or {})}

        async def _call():
            headers = await self._auth_headers()
            if json_body is not None:
                headers["Content-Type"] = "application/json"
            resp = await self._http.request(
                method, url, params=query, json=json_body, headers=headers
            )
            if resp.status_code == 404:
                raise _NotFound(f"{method} {url} -> 404")
            if resp.status_code in (401, 403):
                raise _AuthFailure(f"{method} {url} -> {resp.status_code}")
            resp.raise_for_status()
            if raw_text:
                return resp.text
            if not resp.content:
                return {}
            # ADO returns an HTML sign-in page on some auth failures, with a 200.
            if "application/json" not in resp.headers.get("content-type", ""):
                credential_hint = (
                    "the service principal token is probably invalid or lacks access"
                    if self.auth_mode == "service_principal"
                    else "the PAT is probably invalid or lacks scope"
                )
                raise AzureDevOpsError(
                    f"{method} {url} returned non-JSON ({resp.headers.get('content-type')}); "
                    f"{credential_hint}"
                )
            return resp.json()

        return await with_resilience(
            _call,
            circuit="azure-devops",
            give_up_on=(_NotFound, _AuthFailure, AzureDevOpsError),
        )

    def _repo_path(self, project: str, repository_id: str) -> str:
        return f"{project}/_apis/git/repositories/{repository_id}"

    # ----------------------------------------------------------------------- PRs
    async def get_pull_request(
        self, project: str, repository_id: str, pull_request_id: int
    ) -> dict[str, Any]:
        return await self._request(
            "GET", f"{self._repo_path(project, repository_id)}/pullrequests/{pull_request_id}"
        )

    async def list_active_pull_requests(
        self,
        project: str,
        repository_id: str | None = None,
        *,
        include_drafts: bool = False,
        top: int = 50,
    ) -> list[PullRequestRef]:
        """Active PRs, newest first. The polling trigger's view of the world.

        Scoped to one repository when given an id, otherwise every repository
        in the project.
        """
        path = (
            f"{self._repo_path(project, repository_id)}/pullrequests"
            if repository_id
            else f"{project}/_apis/git/pullrequests"
        )
        data = await self._request(
            "GET",
            path,
            params={"searchCriteria.status": "active", "$top": str(top)},
        )

        refs: list[PullRequestRef] = []
        for raw in data.get("value") or []:
            if raw.get("isDraft") and not include_drafts:
                continue
            pr = pull_request_ref_from_resource(
                raw, default_org_url=self.org_url, default_project=project
            )
            if pr.repository_id and pr.pull_request_id:
                refs.append(pr)
        return refs

    async def get_latest_iteration(
        self, project: str, repository_id: str, pull_request_id: int
    ) -> dict[str, Any] | None:
        data = await self._request(
            "GET",
            f"{self._repo_path(project, repository_id)}/pullRequests/{pull_request_id}/iterations",
        )
        iterations = data.get("value") or []
        return iterations[-1] if iterations else None

    async def get_diff(self, pr: PullRequestRef) -> list[FileDiff]:
        """Materialise the PR diff as per-file hunks.

        Azure DevOps has no unified-diff endpoint, so we take the changed-paths
        list from the latest iteration and reconstruct hunks from file contents
        at the source and target commits. Binary and oversized files are marked
        rather than fetched.
        """
        iteration = await self.get_latest_iteration(
            pr.project, pr.repository_id, pr.pull_request_id
        )
        if not iteration:
            return []

        changes = await self._request(
            "GET",
            f"{self._repo_path(pr.project, pr.repository_id)}"
            f"/pullRequests/{pr.pull_request_id}/iterations/{iteration['id']}/changes",
        )

        source = (iteration.get("sourceRefCommit") or {}).get("commitId") or pr.source_commit
        base = (
            (iteration.get("commonRefCommit") or {}).get("commitId")
            or (iteration.get("targetRefCommit") or {}).get("commitId")
            or pr.target_commit
        )

        diffs: list[FileDiff] = []
        budget = settings.max_diff_bytes
        for entry in (changes.get("changeEntries") or [])[: settings.max_files_per_review]:
            item = entry.get("item") or {}
            path = item.get("path") or ""
            if not path or item.get("isFolder"):
                continue
            change_type = _CHANGE_TYPE_MAP.get(
                str(entry.get("changeType", "edit")).lower(), "edit"
            )
            if _is_binary(path):
                diffs.append(FileDiff(path=path, change_type=change_type, is_binary=True))
                continue
            if budget <= 0:
                diffs.append(FileDiff(path=path, change_type=change_type, truncated=True))
                continue

            after = (
                ""
                if change_type == "delete"
                else await self._safe_content(pr, path, source)
            )
            before = (
                ""
                if change_type == "add"
                else await self._safe_content(pr, path, base)
            )
            budget -= len(after) + len(before)
            diffs.append(_build_file_diff(path, change_type, before, after))
        return diffs

    async def _safe_content(self, pr: PullRequestRef, path: str, commit: str) -> str:
        if not commit:
            return ""
        try:
            return await self.get_file_content(
                pr.project, pr.repository_id, path, commit
            )
        except (_NotFound, AzureDevOpsError):
            return ""

    async def get_file_content(
        self, project: str, repository_id: str, path: str, commit: str
    ) -> str:
        return await self._request(
            "GET",
            f"{self._repo_path(project, repository_id)}/items",
            params={
                "path": path,
                "versionDescriptor.version": commit,
                "versionDescriptor.versionType": "commit",
                "includeContent": "true",
                "$format": "text",
                "download": "false",
            },
            raw_text=True,
        )

    # ------------------------------------------ repository tree (code memory)
    async def get_repository(self, project: str, repository_id: str) -> dict[str, Any]:
        return await self._request("GET", self._repo_path(project, repository_id))

    async def resolve_branch_commit(
        self, project: str, repository_id: str, branch: str
    ) -> str:
        """The commit sha a branch currently points at.

        Locks a tree listing to one consistent commit even if the branch moves
        mid-run - two calls a second apart must still see the same tree.
        """
        data = await self._request(
            "GET",
            f"{self._repo_path(project, repository_id)}/refs",
            params={"filter": f"heads/{branch}"},
        )
        for ref in data.get("value") or []:
            if ref.get("name") == f"refs/heads/{branch}":
                return ref.get("objectId", "")
        raise AzureDevOpsError(f"branch 'refs/heads/{branch}' not found")

    async def list_repository_tree(
        self, project: str, repository_id: str, commit_sha: str
    ) -> list[str]:
        """Every file path in the repository at one commit. Folders excluded."""
        data = await self._request(
            "GET",
            f"{self._repo_path(project, repository_id)}/items",
            params={
                "recursionLevel": "Full",
                "versionDescriptor.version": commit_sha,
                "versionDescriptor.versionType": "commit",
            },
        )
        return [
            item["path"]
            for item in (data.get("value") or [])
            if not item.get("isFolder") and item.get("path")
        ]

    # -------------------------------------------------------------- PR comments
    async def create_comment_thread(
        self,
        pr: PullRequestRef,
        content: str,
        *,
        file_path: str | None = None,
        line: int | None = None,
        status: str = "active",
    ) -> int | None:
        """Post a review comment, anchored to a line when we have one.

        A thread anchored to a file that no longer exists in the iteration is
        rejected by ADO, so anchoring failures fall back to a PR-level comment
        rather than losing the finding.
        """
        body: dict[str, Any] = {
            "comments": [{"parentCommentId": 0, "content": content, "commentType": "text"}],
            "status": status,
        }
        if file_path and line and line > 0:
            body["threadContext"] = {
                "filePath": file_path,
                "rightFileStart": {"line": line, "offset": 1},
                "rightFileEnd": {"line": line, "offset": 1},
            }
        path = (
            f"{self._repo_path(pr.project, pr.repository_id)}"
            f"/pullRequests/{pr.pull_request_id}/threads"
        )
        try:
            created = await self._request("POST", path, json_body=body)
        except AzureDevOpsError:
            if "threadContext" not in body:
                raise
            log.warning("ado.thread.anchor_failed", path=file_path, line=line)
            body.pop("threadContext")
            created = await self._request("POST", path, json_body=body)
        return created.get("id")

    async def list_comment_threads(self, pr: PullRequestRef) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            f"{self._repo_path(pr.project, pr.repository_id)}"
            f"/pullRequests/{pr.pull_request_id}/threads",
        )
        return data.get("value") or []

    # ----------------------------------------------------- Boards / story map
    async def get_pr_work_items(self, pr: PullRequestRef) -> list[int]:
        data = await self._request(
            "GET",
            f"{self._repo_path(pr.project, pr.repository_id)}"
            f"/pullRequests/{pr.pull_request_id}/workitems",
        )
        ids: list[int] = []
        for ref in data.get("value") or []:
            try:
                ids.append(int(ref["id"]))
            except (KeyError, TypeError, ValueError):
                continue
        return ids

    async def get_work_items(self, ids: list[int]) -> list[WorkItem]:
        if not ids:
            return []
        data = await self._request(
            "GET",
            "_apis/wit/workitems",
            params={"ids": ",".join(str(i) for i in ids), "$expand": "relations"},
        )
        items = [_to_work_item(raw) for raw in (data.get("value") or [])]

        # One extra round trip resolves Feature/Epic parents, which is where the
        # story-mapping context actually lives.
        parent_ids = sorted({i.parent_id for i in items if i.parent_id})
        if parent_ids:
            parents = await self._request(
                "GET",
                "_apis/wit/workitems",
                params={"ids": ",".join(str(i) for i in parent_ids)},
            )
            by_id = {
                int(p["id"]): (p.get("fields") or {}) for p in (parents.get("value") or [])
            }
            for item in items:
                fields = by_id.get(item.parent_id or -1) or {}
                item.parent_title = fields.get("System.Title", "")
                item.parent_type = fields.get("System.WorkItemType", "")
        return items

    async def get_delivery_plan(self, project: str) -> dict[str, Any] | None:
        """Resolve the Delivery Plan (the 'storyboard') this project maps against."""
        try:
            data = await self._request(
                "GET", f"{project}/_apis/work/plans", api_version="7.1-preview.1"
            )
        except (_NotFound, AzureDevOpsError, _AuthFailure):
            return None
        plans = data.get("value") or []
        if not plans:
            return None
        if settings.ado_delivery_plan_id:
            return next(
                (p for p in plans if p.get("id") == settings.ado_delivery_plan_id), plans[0]
            )
        if settings.ado_delivery_plan_name:
            return next(
                (p for p in plans if p.get("name") == settings.ado_delivery_plan_name),
                plans[0],
            )
        return plans[0]

    async def get_story_context(self, pr: PullRequestRef) -> StoryContext:
        """Assemble everything the story agent needs, degrading field by field.

        A project with Boards disabled or a PAT without work-item scope must
        still get a code review, so each lookup failure narrows the context
        rather than failing the call.
        """
        try:
            ids = await self.get_pr_work_items(pr)
        except Exception as exc:  # noqa: BLE001
            log.warning("ado.workitems.unavailable", pr=pr.slug, error=str(exc))
            return StoryContext()

        try:
            items = await self.get_work_items(ids)
        except Exception as exc:  # noqa: BLE001
            log.warning("ado.workitem_details.unavailable", pr=pr.slug, error=str(exc))
            items = []

        plan = await self.get_delivery_plan(pr.project)
        iteration = next((i.board_column for i in items if i.board_column), "")
        return StoryContext(
            work_items=items,
            delivery_plan_name=(plan or {}).get("name", ""),
            delivery_plan_teams=_plan_teams(plan),
            iteration_path=iteration,
        )


class _NotFound(AzureDevOpsError):
    pass


class _AuthFailure(AzureDevOpsError):
    pass


# ------------------------------------------------------------------- helpers
def _is_binary(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in _BINARY_EXTENSIONS)


def _to_work_item(raw: dict[str, Any]) -> WorkItem:
    fields = raw.get("fields") or {}
    parent_id: int | None = None
    for rel in raw.get("relations") or []:
        if rel.get("rel") == "System.LinkTypes.Hierarchy-Reverse":
            match = re.search(r"/(\d+)$", rel.get("url", ""))
            if match:
                parent_id = int(match.group(1))
            break
    return WorkItem(
        id=int(raw.get("id", 0)),
        title=fields.get("System.Title", ""),
        work_item_type=fields.get("System.WorkItemType", ""),
        state=fields.get("System.State", ""),
        description=_strip_html(fields.get("System.Description", "")),
        acceptance_criteria=_strip_html(
            fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", "")
        ),
        board_column=fields.get("System.IterationPath", ""),
        parent_id=parent_id,
        url=raw.get("url", ""),
    )


def _plan_teams(plan: dict[str, Any] | None) -> list[str]:
    if not plan:
        return []
    properties = plan.get("properties") or {}
    return [
        t.get("teamName", "")
        for t in (properties.get("teamBacklogMappings") or [])
        if t.get("teamName")
    ]


def _strip_html(value: str) -> str:
    """Boards stores rich-text fields as HTML; models read plain text better."""
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>|</(p|div|li|tr)>", "\n", value, flags=re.I)
    text = re.sub(r"<li>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&quot;": '"', "&#39;": "'",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _build_file_diff(
    path: str, change_type: str, before: str, after: str
) -> FileDiff:
    """Reconstruct hunks with real line numbers so findings can anchor to lines.

    Line numbers are in the *after* file, because that is the coordinate space
    Azure DevOps comment threads use (`rightFileStart`).
    """
    before_lines = before.splitlines()
    after_lines = after.splitlines()

    import difflib

    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    hunks: list[DiffHunk] = []
    added = removed = 0
    context = 3

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1

        start = max(j1 - context, 0)
        end = min(j2 + context, len(after_lines))
        body: list[str] = []
        for idx in range(start, j1):
            body.append(f"{idx + 1:>5}   {after_lines[idx]}")
        for idx in range(i1, i2):
            body.append(f"      - {before_lines[idx]}")
        for idx in range(j1, j2):
            body.append(f"{idx + 1:>5} + {after_lines[idx]}")
        for idx in range(j2, end):
            body.append(f"{idx + 1:>5}   {after_lines[idx]}")

        hunks.append(
            DiffHunk(
                line_start=max(j1 + 1, 1),
                line_end=max(j2, j1 + 1),
                content="\n".join(body),
            )
        )

    return FileDiff(
        path=path,
        change_type=change_type,  # type: ignore[arg-type]
        hunks=_merge_adjacent(hunks),
        added_lines=added,
        removed_lines=removed,
    )


def _merge_adjacent(hunks: list[DiffHunk], gap: int = 8) -> list[DiffHunk]:
    if not hunks:
        return []
    merged = [hunks[0]]
    for hunk in hunks[1:]:
        last = merged[-1]
        if hunk.line_start - last.line_end <= gap:
            merged[-1] = DiffHunk(
                line_start=last.line_start,
                line_end=hunk.line_end,
                content=f"{last.content}\n{hunk.content}",
            )
        else:
            merged.append(hunk)
    return merged


# ---------------------------------------------------------------------- factory
ClientFactory = Callable[[], AzureDevOpsClient]

_factory: ClientFactory = AzureDevOpsClient


def get_ado_client() -> AzureDevOpsClient:
    return _factory()


def set_ado_client_factory(factory: ClientFactory | None) -> None:
    """Install a client factory. Tests use this; production never calls it."""
    global _factory
    _factory = factory or AzureDevOpsClient
