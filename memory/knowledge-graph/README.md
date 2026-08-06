# Knowledge graph — Saul's memory as a map

An Obsidian-style force-graph of the entire memory library: **9,614 nodes,
~25k edges** rendered in WebGL.

- `kg_graph_cli.py` — the exporter. Pulls all memories with embeddings from
  SQLite, builds edges three ways: top-3 cosine similarity per node (chunked
  matmul, RAM-safe), entity-mention regex links, and explicit knowledge-graph
  relations. Assigns each memory a MECE layer (Product / Contacts / Personal)
  by rule + embedding-nearest-anchor.
- `viewer.html` — the static viewer: filter by layer, by memory type, by
  person's network; search dims non-matches; click a node for full text.

## The coverage metric
The headline number answers: *"What % of industry / business / product / user
context does this graph actually contain?"* — scored against 40 canonical
CPO probes, each measured as **depth** (top-5 mean cosine, calibrated on
known-present vs known-absent topics) × **breadth** (how many memories clear
the similarity bar). The first naive version said 91%; calibration against
topics known to be absent brought it to an honest 54%. Selecting a single
domain (Buyer & Seller / Platform / Pay & Ship) swaps in that domain's own
probe set.

The graph.json payload itself isn't committed (it contains raw memory text);
regenerate with the exporter against a live database.
