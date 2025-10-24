#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

# ------------------------------
# Utilidades de normalização
# ------------------------------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def norm_spaces(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def normalize_basename(name: str) -> str:
    s = name.replace("\\", "/")
    s = os.path.basename(s)
    s = s.replace("°", "o").replace("º","o").replace("ª","a")
    s = strip_accents(s).lower()
    s = norm_spaces(s).replace(" - ", " ").replace("_"," ")
    s = re.sub(r'\s+', ' ', s)
    return s

def norm_path(p: str) -> str:
    p = p.replace("\\", "/")
    p = re.sub(r"/+", "/", p).strip()
    return p

# ------------------------------
# Inferências simples
# ------------------------------
def infer_type_from_ext(p: str) -> str:
    ext = os.path.splitext(p)[1].lower()
    return {
        ".docx": "docx",
        ".pdf": "pdf",
        ".md": "md",
        ".json": "json",
    }.get(ext, ext.strip("."))

def find_or_create_collection(idx: dict, prefer_keywords=("Planos de Aula","Planejamentos","Planos")) -> dict:
    cols = idx.get("collections", [])
    # tenta casar por título/id com palavras-chave
    for kw in prefer_keywords:
        for c in cols:
            title = (c.get("title") or "").lower()
            cid = (c.get("id") or "").lower()
            if kw.lower() in title or kw.lower() in cid:
                return c
    # fallback: primeira collection existente
    if cols:
        return cols[0]
    # se não houver collections, cria uma
    newc = {"id":"auto", "title":"Auto (gerada)", "docs":[]}
    idx["collections"] = [newc]
    return newc

def infer_area_from_path(p: str) -> str | None:
    parts = p.split("/")
    # ex.: "Planos de Aula/Robótica/arquivo.docx" → "Robótica"
    try:
        i = parts.index("Planos de Aula")
        if i + 1 < len(parts):
            return parts[i + 1]
    except ValueError:
        pass
    return None

# ------------------------------
# IO do índice
# ------------------------------
def load_index(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def list_repo_files(root: Path, scan_fs_only: bool=False) -> set[str]:
    files = set()
    if not scan_fs_only:
        try:
            out = subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True)
            files = set(norm_path(line) for line in out.splitlines() if line.strip())
        except Exception:
            files = set()
    if scan_fs_only or not files:
        for dirpath, _, filenames in os.walk(root):
            for fn in filenames:
                rel = norm_path(os.path.relpath(os.path.join(dirpath, fn), root))
                if rel.startswith(".git/"):
                    continue
                files.add(rel)
    return files

def flatten_docs(index: dict) -> list[dict]:
    docs = []
    for col in index.get("collections", []):
        col_id = col.get("id")
        col_title = col.get("title")
        for d in col.get("docs", []):
            docs.append({
                "_col_id": col_id,
                "_col_title": col_title,
                "_doc": d,
                "path": norm_path(d.get("path","")),
                "type": d.get("type"),
                "area": d.get("area"),
                "semester": d.get("semester"),
                "year": d.get("year"),
                "grades": tuple(d.get("grades") or []),
                "canonical": bool(d.get("canonical", False)),
            })
    return docs

def apply_rename_table(path: str, table: dict[str,str]) -> str:
    p = path
    for old, new in table.items():
        p = p.replace(old, new)
    return p

# ------------------------------
# Main
# ------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="Raiz do repositório")
    ap.add_argument("--index", default="planos_index.json", help="Caminho do arquivo de índice")
    ap.add_argument("--write", action="store_true", help="Escreve as alterações no arquivo de índice")
    ap.add_argument("--allow-unresolved", action="store_true", help="Não falha mesmo com pendências")
    ap.add_argument("--prefer-folder", default="Planos de Estudos", help="Pasta preferida para canonicidade")
    ap.add_argument("--auto-demote-canonical", action="store_true", help="Desmarca canonical em conflitos")
    ap.add_argument("--auto-add-extras", action="store_true", help="Adiciona ao índice arquivos existentes no repositório que não estão no index")
    ap.add_argument("--report-dir", default="artifacts", help="Diretório para relatórios")
    ap.add_argument("--auto-map-smart", action="store_true", help="Ativa remapeamento inteligente por nome normalizado e heurísticas de pasta")
    ap.add_argument("--use-similarity-fallback", action="store_true", help="Permite escolher melhor candidato por similaridade quando houver múltiplos")
    ap.add_argument("--rename-table", default="", help="JSON com substituições simples de caminho (diretórios)")
    ap.add_argument("--scan-fs-only", action="store_true", help="Força scanner por filesystem em vez de git ls-files")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    index_path = (root / args.index).resolve()
    report_dir = (root / args.report_dir).resolve()

    idx = load_index(index_path)
    docs_before = flatten_docs(idx)
    repo_files = list_repo_files(root, scan_fs_only=args.scan_fs_only)

    # Diagnóstico: salvar lista de arquivos do repo
    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "file_list.txt").open("w", encoding="utf-8") as fl:
        for f in sorted(repo_files):
            fl.write(f + "\n")

    # Auditoria de paths
    missing_before, present = [], []
    for d in docs_before:
        if d["path"] in repo_files:
            present.append(d["path"])
        else:
            missing_before.append(d["path"])

    changed_paths: list[dict] = []

    # Remap exato por basename
    by_name = defaultdict(list)
    for f in repo_files:
        by_name[os.path.basename(f).lower()].append(f)

    remap_exact: dict[str,str] = {}
    for d in docs_before:
        p = d["path"]
        if p in repo_files:
            continue
        fname = os.path.basename(p).lower()
        cands = by_name.get(fname, [])
        if len(cands) == 1:
            remap_exact[p] = cands[0]

    for col in idx.get("collections", []):
        for d in col.get("docs", []):
            p = norm_path(d.get("path",""))
            if p in remap_exact:
                d["path"] = remap_exact[p]
                changed_paths.append({"method":"exact-name","from": p, "to": remap_exact[p]})

    # SMART remap
    if args.auto_map_smart:
        docs_tmp = flatten_docs(idx)
        still_missing_docs = [d for d in docs_tmp if d["path"] not in repo_files]

        smart_index = defaultdict(list)
        for f in repo_files:
            smart_index[normalize_basename(os.path.basename(f))].append(f)

        # rename table
        rename_table = {}
        if args.rename_table:
            try:
                rename_table = json.loads(Path(args.rename_table).read_text(encoding="utf-8"))
            except Exception:
                rename_table = {}

        for d in still_missing_docs:
            old = d["path"]
            candidate_after_rename = apply_rename_table(old, rename_table) if rename_table else old
            if candidate_after_rename in repo_files:
                d["_doc"]["path"] = candidate_after_rename
                changed_paths.append({"method":"rename-table","from": old, "to": candidate_after_rename})
                continue

            key = normalize_basename(os.path.basename(old))
            cands = smart_index.get(key, [])
            chosen = None
            if len(cands) == 1:
                chosen = cands[0]
            elif len(cands) > 1 and args.use_similarity_fallback:
                best, best_score = None, 0.0
                for c in cands:
                    s = SequenceMatcher(a=old.lower(), b=c.lower()).ratio()
                    if args.prefer_folder and args.prefer_folder.lower() in c.lower():
                        s += 0.05
                    base_old = "/".join(old.split("/")[:3])
                    base_new = "/".join(c.split("/")[:3])
                    if base_old and base_new and base_old.lower() == base_new.lower():
                        s += 0.03
                    if s > best_score:
                        best, best_score = c, s
                chosen = best

            if chosen:
                d["_doc"]["path"] = chosen
                changed_paths.append({"method":"smart-name","from": old, "to": chosen})

    # Recomputar missing após remaps
    docs_after_remap = flatten_docs(idx)
    still_missing = [d["path"] for d in docs_after_remap if d["path"] not in repo_files]

    # ---------------------------------------------------------------
    # AUTO-ADD EXTRAS: arquivos do repo que não constam no index
    # ---------------------------------------------------------------
    index_paths_after_remap = set(d["path"] for d in docs_after_remap)
    extras = sorted(f for f in repo_files if f not in index_paths_after_remap)
    auto_added: list[str] = []
    if args.auto_add_extras and extras:
        target_col = find_or_create_collection(idx)
        for f in extras:
            doc = {
                "path": f,
                "canonical": False,
                "type": infer_type_from_ext(f),
            }
            area = infer_area_from_path(f)
            if area:
                doc["area"] = area
            # year/semester/grades propositalmente não definidos
            target_col.setdefault("docs", []).append(doc)
            auto_added.append(f)

    # Após auto-add, consolidar docs finais
    docs_final = flatten_docs(idx)

    # Conflitos de canonicidade
    groups = defaultdict(list)
    for d in docs_final:
        key = (d["area"], d["grades"], d["year"], d["semester"], d["type"])
        if d["canonical"]:
            groups[key].append(d)

    canonical_conflicts = {k:v for k,v in groups.items() if len(v) > 1}
    canonical_resolved = []

    if canonical_conflicts and args.auto_demote_canonical:
        for key, lst in canonical_conflicts.items():
            def score(di):
                p = lst[di]["path"]
                ok = 1 if p in repo_files else 0
                pref = 1 if (args.prefer_folder and args.prefer_folder.lower() in p.lower()) else 0
                return (ok, pref)
            best, bests = 0, (-1,-1)
            for i in range(len(lst)):
                s = score(i)
                if s > bests:
                    best, bests = i, s
            for i, d in enumerate(lst):
                if i == best:
                    continue
                d["_doc"]["canonical"] = False
                canonical_resolved.append({"key": list(key), "demoted_path": d["path"]})

        # Recalcular conflitos
        groups2 = defaultdict(list)
        docs_final = flatten_docs(idx)
        for d in docs_final:
            key = (d["area"], d["grades"], d["year"], d["semester"], d["type"])
            if d["canonical"]:
                groups2[key].append(d)
        canonical_conflicts = {k:v for k,v in groups2.items() if len(v) > 1}

    # ------------------------------
    # Relatórios
    # ------------------------------
    summary = {
        "total_index_docs_before": len(docs_before),
        "total_index_docs_after": len(docs_final),
        "paths_present_before": len([p for p in docs_before if p["path"] in repo_files]),
        "paths_missing_before": len(missing_before),
        "paths_auto_remapped": len([c for c in changed_paths if c["method"] in ("exact-name","smart-name","rename-table")]),
        "paths_missing_after_remap": len(still_missing),
        "auto_added": len(auto_added),
        "canonical_conflicts": len(canonical_conflicts),
        "canonical_auto_demoted": len(canonical_resolved),
        "repo_files_count": len(repo_files),
    }

    report = {
        "summary": summary,
        "changed_paths": changed_paths,
        "still_missing_after_remap": still_missing,
        "auto_added": auto_added,
        "canonical_conflicts_detail": {
            str(k): [d["path"] for d in v] for k, v in canonical_conflicts.items()
        },
        "canonical_auto_demoted": canonical_resolved
    }

    # JSON
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "update_index_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Markdown
    md = [
        "# Relatório de Auditoria do planos_index.json",
        "",
        "## Resumo",
        f"- Docs no index (antes): **{summary['total_index_docs_before']}**",
        f"- Docs no index (depois): **{summary['total_index_docs_after']}**",
        f"- Paths válidos (antes): **{summary['paths_present_before']}**",
        f"- Paths faltando (antes): **{summary['paths_missing_before']}**",
        f"- Paths realocados automaticamente: **{summary['paths_auto_remapped']}**",
        f"- Paths faltando após remap: **{summary['paths_missing_after_remap']}**",
        f"- Arquivos adicionados automaticamente: **{summary['auto_added']}**",
        f"- Conflitos de canonicidade: **{summary['canonical_conflicts']}**",
        f"- Canonicals desmarcados automaticamente: **{summary['canonical_auto_demoted']}**",
        f"- Arquivos encontrados no repo (scanner): **{summary['repo_files_count']}**",
        "",
        "## Paths alterados",
    ]
    if report["changed_paths"]:
        for ch in report["changed_paths"]:
            md.append(f"- ({ch['method']}) `{ch['from']}` → `{ch['to']}`")
    else:
        md.append("- (nenhum)")

    md.append("\n## Paths ainda faltando após remap")
    if still_missing:
        for p in still_missing:
            md.append(f"- `{p}`")
    else:
        md.append("- (nenhum)")

    md.append("\n## Arquivos adicionados automaticamente")
    if auto_added:
        for p in auto_added:
            md.append(f"- `{p}`")
    else:
        md.append("- (nenhum)")

    md.append("\n## Conflitos de canonicidade (restantes)")
    if canonical_conflicts:
        for key, lst in canonical_conflicts.items():
            key_s = str(key)
            for d in lst:
                md.append(f"- {key_s}: `{d['path']}`")
    else:
        md.append("- (nenhum)")

    (report_dir / "update_index_report.md").write_text("\n".join(md), encoding="utf-8")

    # Persistir índice atualizado
    if args.write:
        save_json(idx, index_path)

    # Exit codes “seguros” (não travam se allow-unresolved)
    if summary["paths_missing_after_remap"] > 0 and not args.allow_unresolved:
        print("Há paths pendentes. Revise artifacts/update_index_report.md", file=sys.stderr)
        sys.exit(2)
    if summary["canonical_conflicts"] > 0 and not args.allow_unresolved:
        print("Há conflitos de canonicidade. Revise artifacts/update_index_report.md", file=sys.stderr)
        sys.exit(3)

    print("OK")

if __name__ == "__main__":
    main()
