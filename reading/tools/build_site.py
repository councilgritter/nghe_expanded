"""Export stored articles into the static artifacts the reading page loads.

    python -m reading.tools.build_site

Writes ``reading/data/site/``:

    index.json                 the article list, one entry per (article, level)
    article-<id>-<level>.json  the reading bundle for one article

A bundle carries the simplified text, the token map, the syllable boundaries and the
pre-teach list.  Syllable boundaries are exported because the UI's expand/shrink
handles work in syllables — the token map alone cannot say where the *next* syllable
starts when the current range was segmented wrongly.

Exporting a flat JSON bundle keeps the page fully static, matching how
``build_data.py`` produces ``data.json`` for the listening app: the browser never
needs a backend, and the only network call at read time is fetching this file.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from reading.pipeline.extract import bundle_for_version, list_versions
from reading.settings import settings as default_settings
from reading.storage import db
from reading.storage.dictionary import CompoundDictionary


def export(
    conn,
    out_dir: Path,
    cefr_level: str | None = None,
    source: str | None = None,
    dictionary: CompoundDictionary | None = None,
) -> dict:
    """Write every stored version to ``out_dir``.  Returns a small summary.

    ``dictionary`` is passed through to the bundle builder so each exported article
    carries its offline lookup index.  Without it the export still succeeds — the
    reader just cannot resolve a range the segmenter never produced as a token.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []

    for row in list_versions(conn, cefr_level=cefr_level, source=source):
        bundle = bundle_for_version(conn, row["id"], dictionary=dictionary)
        if bundle is None:
            continue
        filename = f"article-{row['article_id']}-{row['cefr_level']}.json"
        (out_dir / filename).write_text(
            json.dumps(bundle, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        article = bundle["article"]
        version = bundle["version"]
        exercises = bundle.get("exercises") or {}
        entries.append(
            {
                "article_id": row["article_id"],
                "cefr_level": row["cefr_level"],
                "file": filename,
                "source": article["source"],
                "title": article["title"],
                "url": article["source_url"],
                "published_at": article["published_at"],
                "attribution": article["attribution"],
                "syllables": len(bundle["syllables"]),
                "tokens": len(bundle["tokens"]),
                "ambiguous": sum(1 for t in bundle["tokens"] if t["ambiguous"]),
                "lookup": len(bundle.get("lookup") or []),
                "questions": len(exercises.get("mcq") or [])
                + len(exercises.get("short") or []),
                "writing": bool(exercises.get("writing")),
                "vocab": len(bundle["preteach"]["vocab"]),
                "grammar": len(bundle["preteach"]["grammar"]),
                "quality": version["quality"],
            }
        )

    entries.sort(key=lambda e: (e["cefr_level"], e["published_at"] or "", e["title"]))
    manifest = {
        "generated_by": "reading.tools.build_site",
        "levels": sorted({e["cefr_level"] for e in entries}),
        "articles": entries,
    }
    (out_dir / "index.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return {
        "entries": len(entries),
        "levels": manifest["levels"],
        "out_dir": str(out_dir),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="output directory")
    parser.add_argument("--cefr", default=None, help="export only one level")
    parser.add_argument("--source", default=None, choices=["voa", "bbc"])
    args = parser.parse_args(argv)

    out_dir = Path(args.out) if args.out else default_settings.site_dir
    try:
        dictionary = CompoundDictionary.from_sqlite(
            default_settings.dict_path,
            max_syllables=default_settings.max_compound_syllables,
        )
    except FileNotFoundError as exc:
        # Not fatal: the bundles simply carry no lookup index, and the reader falls
        # back to the token map alone (exactly how it behaved before the index).
        print(f"warning: {exc}\nExporting without the offline lookup index.")
        dictionary = None

    conn = db.connect(default_settings.db_path)
    db.migrate(conn)
    try:
        summary = export(
            conn, out_dir, cefr_level=args.cefr, source=args.source, dictionary=dictionary
        )
    finally:
        conn.close()

    print(f"{summary['entries']} article bundles, levels: {', '.join(summary['levels']) or 'none'}")
    print(f"wrote {out_dir}")
    if dictionary is not None:
        print(f"lookup index built from {len(dictionary):,} dictionary headwords")
    if not summary["entries"]:
        print("nothing to export — run the ingester first:")
        print("    python -m reading.pipeline.ingest --cefr B1 --limit 5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
