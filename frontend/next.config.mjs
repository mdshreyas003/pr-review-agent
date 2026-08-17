import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output keeps the container image small: Next traces the modules
  // actually reachable at runtime instead of shipping all of node_modules.
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  // A stray lockfile above the repo makes Next guess the workspace root
  // wrong; pin it so that guess never changes silently.
  turbopack: {
    root: __dirname,
  },
};

export default nextConfig;
