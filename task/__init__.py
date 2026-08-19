"""v4.0 Task Execution Layer — autobiographical task system on top of v3.0 memory.

Architecture:
  L0 · Self          → model weights + v3.0 memory = persistent self
  L1 · Focus         → sliding window (5 blocks), the current attention
  L2 · Externalized  → work memory + DAG, task-scoped artifacts
  L3 · Long-term     → v3.0 memory (unchanged)

Core philosophy: single self, sliding attention, externalized artifacts.
"""
