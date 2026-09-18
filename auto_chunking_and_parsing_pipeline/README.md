# Technical Manuals RAG Pipeline

A Databricks Asset Bundle (DAB) that turns PDF manuals stored in a Unity Catalog **Volume** into a searchable **Vector Search index**, ready to power a RAG agent.

Each run:
1. **Parses** the PDFs with `ai_parse_document` (text + tables), chunks them with `ai_prep_search`, and enriches image/diagram pages with a multimodal model (`ai_query`).
2. **Indexes** the resulting chunks into a Databricks **Vector Search** delta-sync index (managed embeddings via `databricks-gte-large-en`).

The bundle ships **two jobs** — one per document collection — that share a single Vector Search endpoint.

---

## 1. Variables you need to modify

All environment-specific values are exposed as **bundle variables** in [`databricks.yml`](databricks.yml). A client adopting this bundle only needs to change these — the notebooks read them as job parameters, so no code edits are required.

| Variable | What it is | Default | Change to... |
|---|---|---|---|
| `catalog` | Unity Catalog that holds the tables, volumes and the Vector Search index. | `stable_classic_6kvrb7_catalog` | Your catalog |
| `schema` | Schema (database) inside the catalog. | `cesar_cordoba` | Your schema |
| `device_volume` | Volume (folder) containing the **device** manuals PDFs. | `device_manuals` | Your device-manuals volume |
| `gov_volume` | Volume (folder) containing the **government** manuals PDFs. | `gov_manual` | Your gov-manuals volume |

You also need to point the bundle at your workspace — edit the `host` under `targets.dev.workspace` (and set `targets.dev.variables.catalog` / `schema` if you want per-target overrides):

```yaml
targets:
  dev:
    workspace:
      host: https://<your-workspace>.cloud.databricks.com
    variables:
      catalog: <your_catalog>
      schema: <your_schema>
```

> **Prerequisite:** the two volumes must already exist and contain the PDFs, i.e. `/Volumes/<catalog>/<schema>/<device_volume>/*.pdf` and `.../<gov_volume>/*.pdf`. The pipeline reads whatever PDFs are in those volumes.

### The one fixed value

The Vector Search endpoint name is **fixed** to `technical_manuals_vs_endpoint` (see [`resources/ai_search_endpoint.yml`](resources/ai_search_endpoint.yml)). Both jobs share it. Change it there only if you want a different endpoint name.

### Overriding without editing files

You can override any variable at deploy/run time:

```bash
databricks bundle deploy --var="catalog=my_catalog,schema=my_schema,device_volume=my_devices"
```

---

## 2. Code structure

```
auto_chunking_and_parsing_pipeline/
├── databricks.yml                      # Bundle definition: name, variables, targets (dev/prod)
├── resources/                          # DAB resources (auto-included via include: resources/*.yml)
│   ├── ai_search_endpoint.yml          # Vector Search endpoint: technical_manuals_vs_endpoint (shared)
│   ├── device_rag.job.yml              # Job for the device manuals  (uses ${var.device_volume})
│   └── gov_rag.job.yml                 # Job for the government manuals (uses ${var.gov_volume})
├── job_code/                           # Notebooks executed by the jobs
│   ├── 01-setup.py                     # Helper funcs (endpoint/index exists + wait-for-ready)
│   ├── 02-parse pdf for ai search.ipynb   # TASK 1: parse + chunk + image enrichment
│   ├── 03-sync ai search.ipynb            # TASK 2: create endpoint + create/sync VS index
│   └── explanation/                    # Walkthrough notebook + images (excluded from deploy)
└── manuals/                            # Local sample PDFs (for reference; data actually lives in UC volumes)
```

### The two jobs

Both jobs are identical in shape and differ only in which volume they target:

| Job resource | Job name | Volume parameter |
|---|---|---|
| `device_manuals_rag` | `device-manuals-rag-pipeline` | `${var.device_volume}` |
| `gov_manuals_rag` | `gov-manuals-rag-pipeline` | `${var.gov_volume}` |

Each job has two serverless tasks that run in sequence:

```
parse_documents  →  index_vector_search
 (notebook 02)        (notebook 03)
```

Both tasks receive `catalog`, `schema`, `volume` as parameters; `index_vector_search` also receives `vector_search_endpoint` (wired to the shared endpoint resource).

### What each notebook does

- **`01-setup.py`** — utility functions (`endpoint_exists`, `index_exists`, `wait_for_vs_endpoint_to_be_ready`, `wait_for_index_to_be_ready`). Run via `%run` from notebook 03.
- **`02-parse pdf for ai search.ipynb`** — reads the PDFs from the volume, runs `ai_parse_document` + `ai_prep_search` to produce text chunks, extracts page images, describes any diagrams/figures with a multimodal `ai_query`, then joins chunks + visual descriptions into the final parsed table (Change Data Feed enabled).
- **`03-sync ai search.ipynb`** — creates the Vector Search endpoint if needed, then creates (or triggers a sync of) a **delta-sync** index over the parsed table. Embeddings are computed automatically from the `chunk_to_embed` column.

---

## 3. What gets created

Names are **prefixed by the volume**, so the two jobs never collide even though they share one endpoint. For a given `{volume}`:

| Object | Fully-qualified name | Created by | Purpose |
|---|---|---|---|
| Table | `{catalog}.{schema}.{volume}_chunked_document_view` | notebook 02 | Parsed + chunked text (one row per chunk) |
| Table | `{catalog}.{schema}.{volume}_image_visuals` | notebook 02 | Multimodal descriptions of diagrams/figures |
| Table | `{catalog}.{schema}.{volume}_document_with_images_parsed` | notebook 02 | Final table indexed by Vector Search (CDF enabled) |
| Files | `/Volumes/{catalog}/{schema}/{volume}/images/` | notebook 02 | Page images emitted by `ai_parse_document` |
| VS endpoint | `technical_manuals_vs_endpoint` | DAB + notebook 03 | Serves the indexes (shared by both jobs) |
| VS index | `{catalog}.{schema}.{volume}_knowledge_base_vs_index` | notebook 03 | Delta-sync index queried by the RAG agent |

Example for the defaults: `..._document_with_images_parsed` becomes `stable_classic_6kvrb7_catalog.cesar_cordoba.device_manuals_document_with_images_parsed`, indexed as `..._knowledge_base_vs_index`.

> The index is created with `pipeline_type="TRIGGERED"`, so it re-embeds only changed rows (via Change Data Feed) and only when the `index_vector_search` task runs — not automatically on every write to the table.

---

## 4. Deploy & run

```bash
cd public_demo/auto_chunking_and_parsing_pipeline

# validate the bundle
databricks bundle validate

# deploy jobs + endpoint to the workspace
databricks bundle deploy

# run a pipeline (choose the collection you want)
databricks bundle run device_manuals_rag
databricks bundle run gov_manuals_rag
```

Everything runs on **serverless compute** (no cluster config), so the bundle is portable across workspaces — `ai_parse_document`, `ai_prep_search`, `ai_query`, and Vector Search are all available there.
