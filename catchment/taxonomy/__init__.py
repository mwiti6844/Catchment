"""Taxonomy maintenance: executing the changes a human has approved.

Proposing lives in the classifier and the repository layer; this package holds
the other half — the code that consumes an approved proposal and rewrites the
graph. Kept separate so the boundary in ``docs/taxonomy.md`` stays visible: the
classifier can only ever write to the review queue, and nothing here runs
without a recorded approval.
"""
