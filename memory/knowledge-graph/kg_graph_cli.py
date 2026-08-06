"""Export Saul's memory library as an Obsidian-style knowledge graph.

Usage: python3 -m goodman.kg_graph_cli [--out DIR] [--topk 3] [--min-sim 0.55]

Writes graph.json (nodes + edges) into the output dir (default data/graph/).
Read-only against goodman.db.
"""

import argparse
import json
import re
import sqlite3
from pathlib import Path

import numpy as np

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "goodman.db"
OUT_DIR = Path(__file__).resolve().parents[2] / "data" / "graph"

EXCLUDED_CATEGORIES = ("document", "book")
CONTENT_CAP = 600
CHUNK = 512

PERSONAL_CATEGORIES = {"health", "finance", "joel-revelations"}

LAYER_ANCHORS = {
    "product": (
        "ACME Market classifieds marketplace work: product strategy, revenue, "
        "MAU metrics, listings, verticals real estate auto goods, buyers and sellers, "
        "monetization pricing, engineering team processes, hiring recruiting for ACME, "
        "board reporting, competitors Avito, design system, roadmap, meetings at work"
    ),
    "personal": (
        "Personal life outside work: stock investments portfolio trading NVDA AMZN, "
        "personal taxes and residency permits Lithuania, health fitness sleep supplements "
        "longevity, travel trips flights hotels mountains vacations, family and friends, "
        "books reading, personal finance savings, spiritual insights revelations"
    ),
}

# 40 canonical CPO context probes; coverage = strongest nearby Product memory
CONTEXT_PROBES = {
    "industry": [
        "size and growth of the classifieds market in Kazakhstan",
        "Avito entering the Kazakhstan market as a competitor",
        "real estate market in Almaty: new developments and developers",
        "used car and auto market dynamics in Kazakhstan",
        "how classifieds marketplaces monetize: listing fees, packages, ads",
        "SEO and organic acquisition strategies for classifieds platforms",
        "local competitor classifieds and vertical platforms in Kazakhstan",
        "payments and fintech landscape in Kazakhstan",
        "regulation and legal environment for online platforms in Kazakhstan",
        "macroeconomic situation in Kazakhstan: incomes, inflation, currency",
    ],
    "business": [
        "ACME Market monthly revenue and MAU dynamics",
        "staff costs and payroll budget of ACME Market",
        "unit economics and business model of ACME Market",
        "pricing and packages for sellers on ACME",
        "EBITDA and margin structure of the business",
        "hiring plan and organizational structure of the company",
        "company strategy and goals for 2026",
        "key business KPIs tracked in weekly business review",
        "marketing budget and campaign spending",
        "carve-out, TSA and transition costs of the new organization",
    ],
    "product": [
        "product roadmap and feature priorities for ACME Market",
        "search, discovery and trust and safety product areas",
        "new developments catalog product for real estate",
        "conversion rate funnel from listing view to buyer contact by vertical",
        "top search queries and zero-results rate in the ACME search engine",
        "A/B test results and experiment outcomes for product features",
        "mobile app quality and app store reviews",
        "payments and delivery pay and ship product",
        "AI features and automation in the product",
        "feature adoption and usage metrics for ACME features",
    ],
    "user": [
        "how buyers search and choose items on ACME",
        "how sellers and realtors list and promote on ACME",
        "customer development interviews with users",
        "user complaints and pain points on the platform",
        "scams and fraud affecting users on classifieds",
        "listing liquidity: average days to sell and sell-through rate by category",
        "buyer retention cohorts and repeat purchase frequency data",
        "user satisfaction and NPS feedback",
        "user segments and their differences in Kazakhstan",
        "Telegram and other channels users prefer for buying and selling",
    ],
}


# Per-domain context probes; same calibration as CONTEXT_PROBES
DOMAIN_PROBES = {
    "petrov": {  # Buyer & Seller Experience
        "buyer": [
            "how buyers search and choose items on ACME",
            "buyer conversion funnel from listing view to contact",
            "buyer complaints and pain points on the platform",
            "buyer retention and repeat purchase behavior",
            "top search queries of buyers on classifieds",
        ],
        "seller": [
            "how sellers list and promote items on ACME",
            "seller experience of posting a listing",
            "realtors and professional sellers on ACME",
            "seller promotion tools and paid packages",
            "seller churn and seller satisfaction",
        ],
        "trust": [
            "ratings and reviews of sellers on the platform",
            "scams and fraud affecting buyers and sellers",
            "trust and safety of listings",
            "moderation and quality of listing content",
            "user complaints about dishonest counterparties",
        ],
        "catalog": [
            "new developments catalog product for real estate",
            "real estate developers and new buildings in Almaty",
            "auto catalog and vehicle listings",
            "structured attributes and catalog data of listings",
            "vertical-specific listing experience for real estate and auto",
        ],
    },
    "kovalev": {  # Platform
        "search": [
            "search engine relevance and ranking on ACME",
            "recommendations and discovery feed",
            "zero results rate and search quality",
            "search query analytics and logs",
            "personalization of search results",
        ],
        "safety": [
            "scam and fraud prevention on the platform",
            "moderation of listings and users",
            "user verification and identity checks",
            "phishing and off-platform fraud schemes",
            "blocking and banning bad actors",
        ],
        "messaging": [
            "in-app messenger and chat between buyers and sellers",
            "chat response rates and communication quality",
            "notifications and engagement messaging",
            "Telegram and external channels users prefer for deals",
            "spam in messages on the platform",
        ],
        "monetize": [
            "pricing and packages for sellers on ACME",
            "listing fees and paid promotion products",
            "monetization strategy and ARPU",
            "advertising products and display ads",
            "willingness to pay of sellers",
        ],
    },
    "dana": {  # Pay & Ship
        "payments": [
            "online payments on a classifieds platform",
            "payment providers and fintech landscape in Kazakhstan",
            "escrow and secure payment between buyer and seller",
            "payment conversion and failure rates",
            "Payme Click Uzum payment systems in Kazakhstan",
        ],
        "delivery": [
            "delivery and shipping of purchased goods",
            "courier and logistics providers in Kazakhstan",
            "pay and ship product for classifieds",
            "delivery cost and speed expectations of users",
            "returns and disputes for delivered goods",
        ],
        "economics": [
            "transaction take rate and commission model",
            "C2C and B2C transactional marketplace model",
            "cross-pollination from classifieds to transactions",
            "unit economics of pay and ship",
            "fraud and chargebacks in online transactions",
        ],
        "adoption": [
            "user adoption of pay and ship features",
            "trust in online payment among Kazakh users",
            "seller onboarding to a transactional flow",
            "buyer willingness to pay online instead of cash",
            "cash on delivery preferences in Kazakhstan",
        ],
    },
}


PEOPLE = {
    "petrov": r"petrov\w*|купцов\w*|vadim\.petrov",
    "kovalev": r"kulbatski\w*|кульбацк\w*|andrei\.kovalev",
    "dana": r"dana\w*|камол\w*|abdullaeva\w*|абдуллаев\w*",
}


def tag_people(contents):
    pats = {k: re.compile(rf"(?<!\w)(?:{v})", re.IGNORECASE) for k, v in PEOPLE.items()}
    tags = []
    for text in contents:
        tags.append([k for k, p in pats.items() if p.search(text)])
    return tags


def assign_layers(cats, mat):
    """MECE layer per memory: contacts (rule), personal (rule or anchor), product."""
    from goodman.embeddings import embed_batch

    anchors = np.asarray(
        embed_batch([LAYER_ANCHORS["product"], LAYER_ANCHORS["personal"]]),
        dtype=np.float32,
    )
    sims = mat @ anchors.T  # (n, 2)
    layers = []
    for i, cat in enumerate(cats):
        if cat == "contact":
            layers.append("contacts")
        elif cat in PERSONAL_CATEGORIES:
            layers.append("personal")
        else:
            layers.append("personal" if sims[i, 1] > sims[i, 0] else "product")
    return layers


def probe_coverage(prod, probes_by_dim):
    """Coverage of context probes by Product-layer memories, 0-100."""
    from goodman.embeddings import embed_batch

    # depth x breadth, calibrated on known-present vs known-absent probes:
    # present topics have top5mean ~0.65-0.72 and 29-172 memories >=0.5;
    # absent ones top5mean ~0.46-0.56 and 0-5 memories. Max-cosine alone inflates.
    lo, hi = 0.45, 0.65
    dims = {}
    for dim, probes in probes_by_dim.items():
        pv = np.asarray(embed_batch(probes), dtype=np.float32)
        sims = prod @ pv.T  # (n_prod, n_probes)
        scores = []
        for p in range(sims.shape[1]):
            col = sims[:, p]
            top5 = np.sort(col)[-5:]
            depth = float(np.clip((top5.mean() - lo) / (hi - lo), 0, 1))
            breadth = float(min(1.0, (col >= 0.5).sum() / 8))
            scores.append(depth * breadth)
        dims[dim] = round(sum(scores) / len(scores) * 100)
    overall = round(sum(dims.values()) / len(dims))
    return {"overall": overall, "dims": dims}


def load_memories(db):
    q = f"""SELECT id, category, content, created_at, embedding FROM memories
            WHERE category NOT IN ({','.join('?' * len(EXCLUDED_CATEGORIES))})
              AND embedding IS NOT NULL"""
    rows = db.execute(q, EXCLUDED_CATEGORIES).fetchall()
    ids, cats, contents, dates, vecs = [], [], [], [], []
    for mid, cat, content, created, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        if v.shape[0] != 384:
            continue
        ids.append(mid)
        cats.append(cat)
        contents.append(content)
        dates.append((created or "")[:10])
        vecs.append(v)
    return ids, cats, contents, dates, np.vstack(vecs)


def similarity_edges(mat, topk, min_sim):
    n = mat.shape[0]
    edges = set()
    for start in range(0, n, CHUNK):
        block = mat[start : start + CHUNK] @ mat.T  # (chunk, n)
        for i in range(block.shape[0]):
            gi = start + i
            row = block[i]
            row[gi] = -1.0
            for j in np.argpartition(row, -topk)[-topk:]:
                if row[j] >= min_sim:
                    a, b = (gi, int(j)) if gi < j else (int(j), gi)
                    edges.add((a, b, round(float(row[j]), 3)))
    return edges


def entity_links(db, contents):
    """Match kg entity names/aliases inside memory contents (word-boundary)."""
    names = {}  # entity_id -> [name, alias...]
    ents = db.execute("SELECT id, name, entity_type FROM kg_entities").fetchall()
    for eid, name, _ in ents:
        names[eid] = [name]
    for eid, alias in db.execute("SELECT entity_id, alias FROM kg_aliases"):
        if eid in names:
            names[eid].append(alias)
    patterns = {}
    for eid, variants in names.items():
        toks = [re.escape(v) for v in variants if v and len(v) >= 4]
        if toks:
            patterns[eid] = re.compile(
                r"(?<!\w)(" + "|".join(toks) + r")(?!\w)", re.IGNORECASE
            )
    links = []  # (mem_index, entity_id)
    for idx, text in enumerate(contents):
        for eid, pat in patterns.items():
            if pat.search(text):
                links.append((idx, eid))
    return ents, links


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--min-sim", type=float, default=0.55)
    args = ap.parse_args()

    db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    ids, cats, contents, dates, mat = load_memories(db)
    print(f"memories: {len(ids)}")

    edges = similarity_edges(mat, args.topk, args.min_sim)
    print(f"similarity edges: {len(edges)}")

    ents, links = entity_links(db, contents)
    rels = db.execute(
        "SELECT source_id, target_id, rel_type FROM kg_relationships"
    ).fetchall()
    print(f"entities: {len(ents)}, mem-entity links: {len(links)}, entity rels: {len(rels)}")

    layers = assign_layers(cats, mat)
    prod = mat[np.array([l == "product" for l in layers])]
    metric = probe_coverage(prod, CONTEXT_PROBES)
    domain_metrics = {k: probe_coverage(prod, v) for k, v in DOMAIN_PROBES.items()}
    people = tag_people(contents)
    from collections import Counter
    print(f"layers: {Counter(layers)}")
    print(f"context metric: {metric}")
    print(f"domain metrics: {domain_metrics}")
    print(f"people: {Counter(p for tags in people for p in tags)}")

    nodes = []
    for i, (mid, cat) in enumerate(zip(ids, cats)):
        node = {
            "id": f"m{mid}",
            "type": cat,
            "layer": layers[i],
            "label": contents[i][:80],
            "text": contents[i][:CONTENT_CAP],
            "date": dates[i],
        }
        if people[i]:
            node["pm"] = people[i]
        nodes.append(node)
    ent_ids = set()
    ent_tags = tag_people([name for _, name, _ in ents])
    for (eid, name, etype), etag in zip(ents, ent_tags):
        node = {"id": f"e{eid}", "type": "entity",
                "layer": "contacts" if etype == "person" else "product",
                "label": name, "text": f"[{etype}] {name}", "date": ""}
        if etag:
            node["pm"] = etag
        nodes.append(node)
        ent_ids.add(eid)

    out_edges = [
        {"source": f"m{ids[a]}", "target": f"m{ids[b]}", "w": w}
        for a, b, w in edges
    ]
    out_edges += [
        {"source": f"m{ids[i]}", "target": f"e{eid}", "w": 0.9}
        for i, eid in links
    ]
    out_edges += [
        {"source": f"e{s}", "target": f"e{t}", "w": 1.0, "rel": r}
        for s, t, r in rels
        if s in ent_ids and t in ent_ids
    ]

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    payload = {"nodes": nodes, "links": out_edges, "metric": metric,
               "domain_metrics": domain_metrics}
    (out / "graph.json").write_text(json.dumps(payload, ensure_ascii=False))
    size = (out / "graph.json").stat().st_size
    print(f"wrote {out / 'graph.json'} ({size/1e6:.1f} MB, "
          f"{len(nodes)} nodes, {len(out_edges)} edges)")


if __name__ == "__main__":
    main()
