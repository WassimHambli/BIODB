# biodb-client

A typed Python client for four public biology databases. Install it as `biodb-client`, import it as
`biodb`.

| Service | What you can get |
|---|---|
| **UniProt** | entries, FASTA, search |
| **PDB** (RCSB) | entries, polymer entities, text search, mmCIF files |
| **Ensembl** | gene, transcript and protein lookup, sequences, cross-references, archive |
| **NCBI** (E-utilities) | `esearch`, `esummary`, `efetch` |

All four share one HTTP layer with per-host rate limiting, retries with backoff, an on-disk cache,
identifier checks, validated responses, and one family of exceptions. It also recognises records that
look fine but aren't: deleted UniProt accessions, obsolete PDB entries, retired Ensembl IDs, and NCBI
errors returned with HTTP 200.

**Status:** beta (0.1.0). Not affiliated with or endorsed by any of these services.

## Install

Python 3.11 or newer. Not on PyPI: **the PyPI project named `biodb` is unrelated, so don't
`pip install biodb`.** Download or clone this repository, then from its folder (check `python3 --version` first: the Python built into macOS is too old, so you may
need `python3.13` or similar in the first command):

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

For development, use `pip install -e ".[dev]"`. Don't install this in the same environment as the
unrelated `biodb` package.

## Usage

Outputs below are from a live run on 2026-11-22. Upstream data changes over time.

```python
from biodb import BioDB

with BioDB() as db:
    # UniProt
    entry = db.uniprot.get_entry("P69905")
    print(entry.accession, entry.entry_name, entry.gene_names, entry.length)
    # P69905 HBA_HUMAN ['HBA1', 'HBA2'] 142
    for hit in db.uniprot.search("gene:HBA1 AND organism_id:9606 AND reviewed:true", max_results=3):
        print(hit.accession)
        # P69905

    # PDB
    structure = db.pdb.get_entry("4HHB")
    print(structure.method, structure.resolution, structure.polymer_entity_ids)
    # X-RAY DIFFRACTION 1.74 ['1', '2']
    print(db.pdb.get_polymer_entity("4HHB", 1).uniprot_ids)
    # ['P69905']
    path = db.pdb.download_mmcif("4HHB", "4HHB.cif")  # returns a pathlib.Path

    # Ensembl
    gene = db.ensembl.lookup_symbol("homo_sapiens", "BRCA2")
    print(gene.id, gene.seq_region_name, gene.start, gene.end, gene.assembly_name)
    # ENSG00000139618 13 32315086 32400268 GRCh38
    print(db.ensembl.get_sequence("ENSP00000369497", "protein").seq[:20])
    # MPIGSKERPTFFEIFKTRCN

    # NCBI
    found = db.ncbi.esearch("gene", "BRCA1[sym] AND human[orgn]")
    print(found.ids)
    # ['672']
    print(db.ncbi.esummary("gene", found.ids)["672"].raw["maplocation"])
    # 17q21.31
    print(db.ncbi.efetch("nuccore", "NM_007294", "fasta", "text").splitlines()[0])
    # >NM_007294.4 Homo sapiens BRCA1 DNA repair associated (BRCA1), transcript variant 1, mRNA
```

Every model keeps the unmodified JSON in `.raw`.

### Errors

Request failures and rejected identifiers raise subclasses of `BiodbError`, with `.service`, `.url`,
`.status` and `.body_excerpt`.

```python
from biodb import BioDB, InactiveEntryError, NotFoundError, ObsoleteEntryError

with BioDB() as db:
    try:
        db.pdb.get_entry("1HHB")
    except ObsoleteEntryError as err:
        print(err.replaced_by)
        # ('4HHB',)
    try:
        db.uniprot.get_entry("P62988")
    except InactiveEntryError as err:
        print(err.reason, err.replaced_by)
        # DEMERGED ('P0CG47', 'P0CG48', 'P62979', 'P62987')
    try:
        db.uniprot.get_entry("Q00000")
    except NotFoundError as err:
        print(err.status)
        # 404
```

| Exception | When |
|---|---|
| `InvalidIdentifierError`, `InvalidParameterError` | rejected before any request |
| `NotFoundError` | no such record; also empty UniProt FASTA, blank `efetch`, UIDs without a summary |
| `InactiveEntryError`, `ObsoleteEntryError`, `RetiredIdentifierError` | subclasses of `NotFoundError` for deleted or demerged UniProt, removed PDB, and retired Ensembl records |
| `BadRequestError` | other 4xx, an NCBI error inside an HTTP 200 body, or an empty query or ID list (no request made) |
| `RateLimitError` / `ServerError` | 429 / 5xx after retries, or the server asked for a longer wait than the 30 s cap (`.retry_after`) |
| `TransportError` | network failure, or a refused redirect |
| `ResponseValidationError` | the body isn't what the service should return |

Errors from your own file system in `download_mmcif`, and `ValueError` for invalid configuration, are
ordinary Python exceptions. See [docs/FAILURE_MODES.md](docs/FAILURE_MODES.md) for 51 observed cases.

### Configuration

```python
from biodb import BioDB, BioDBConfig

config = BioDBConfig(
    cache_path="my-cache.sqlite",  # default: $BIODB_CACHE_PATH or ~/.cache/biodb/cache.sqlite
    cache_ttl=24 * 3600,  # seconds; default 7 days
    cache_enabled=True,
    rate_limits={"rest.ensembl.org": 5},  # requests per second, per host
    max_attempts=5,
    timeout=30.0,
)
db = BioDB(config)
db.close()
```

NCBI asks heavy users to identify themselves. Set `NCBI_EMAIL` and `NCBI_API_KEY` in the environment, or
`ncbi_email` and `ncbi_api_key` in the config. They're sent only to NCBI, in a POST body, and are redacted
from biodb's error messages and logs. Tools that print local variables in tracebacks (such as
`pytest --showlocals` or some error trackers) can still show them. A key raises the limit from 3 to 10 requests per second.

## How it behaves

- **Rate limits:** evenly spaced requests per host: NCBI 3/s (10/s with a key), Ensembl 15/s, UniProt
  10/s, RCSB 10/s. The UniProt and RCSB values are our own conservative choice. Actual spacing is
  slightly slower.
- **Retries:** up to 5 attempts with jittered backoff on 429, 500, 502, 503, 504 and network errors.
  `Retry-After` up to 30 s is respected; a longer wait raises at once.
- **Cache:** SQLite, successful responses only, stored after validation. Credentials aren't stored.
  `cache=False` or `ttl=seconds` override it per call. If the cache file is unusable, biodb logs a
  warning and carries on without it.
- **Redirects:** followed for GET requests on the same host only.

## Limitations

- Synchronous only. Rate limits apply per `BioDB` object and process, so parallel processes add up.
  RCSB's three hosts are limited separately.
- Identifier checks catch malformed IDs, not every wrong one. Ensembl IDs are checked loosely because
  species use many formats.
- `rest.ensembl.org` only; separate Ensembl Genomes sites aren't covered. Only the current version of an
  Ensembl ID can be fetched, and versions aren't checked when a transcript ID returns a protein.
- NCBI silently rewrites some malformed searches. Compare `esearch(...).query_translation` with your term.
  An unknown `efetch` `rettype` only logs a warning, because NCBI falls back to a default format.
- `esummary` raises if any requested UID has no summary. `efetch` refuses more than 200 IDs in XML or ASN.1.
- A long UniProt search can mix cached and fresh pages across a release, or fail if a cached page's
  cursor has expired (use `cache=False`). `search_text` covers PDB full-text
  search only, up to 10,000 hits.
- `download_mmcif` holds the file in memory and overwrites by default (`overwrite=False` to refuse).
- The cache isn't pruned automatically (`Cache.purge_expired()`).
- **Not verified:** a live 429 response and a real NCBI API key were never exercised; both are covered by
  offline tests only. Developed and tested on macOS with Python 3.13; the CI file also covers 3.11 and 3.12
  on Linux, so check its result.

## Testing

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy --strict src
pytest              # offline; needs no network, keys or accounts
pytest -m live      # optional; calls the real public services
```

## Data sources and licence
