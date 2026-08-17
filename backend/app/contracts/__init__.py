"""The contract layer: shared data models, agent I/O schemas, the LLM protocol,
the Azure DevOps integration, and the two triggers (webhook + poller) that feed
work into the system. Nothing in here runs a review; it defines the shapes and
the boundary the rest of the app talks through.
"""
