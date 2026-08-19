"""
全能王多设备同步子系统
=======================
Hash-based incremental sync for chat.db + memory fragments.
No third-party sync software — runs on Aliyun ECS.

Architecture:
  Each PC: sync_client.py polls local chat.db, computes row-level SHA-256 hashes,
           pushes diffs to Aliyun sync_server, pulls remote changes.
  Aliyun:  sync_server.py runs on ECS, stores hash tree + canonical chat.db snapshot,
           resolves conflicts by last-write-wins timestamp.
  Phone:   syncs via same Aliyun API (mobile SDK or HTTP client).

Sync Protocol:
  For each synced table (topics, messages, memory_fragments):
    1. Compute SHA-256(row_id + row_json) for every row
    2. Send hash_map = {row_id: sha256_hex} to server
    3. Server responds with rows_we_need = ids where hash differs or is missing
    4. Client sends full row data for rows_we_need
    5. Server merges (newer ts wins), stores updated hash_tree
    6. Server returns rows it has that client is missing
    7. Client applies remote changes locally
"""
