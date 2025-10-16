#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_index_v2.py — Auditoria e atualização inteligente do planos_index.json

Novidades vs v1:
- Auto-mapeamento "smart" por **nome de arquivo normalizado** (acentos, caixa, espaços, º vs o).
- Heurística de pasta preferida (por padrão "Planos de Estudos") para desempate.
- Similaridade por SequenceMatcher como fallback (opcional).
- Suporte a tabela de renomeação de diretórios (--rename-table).

Exemplo de uso recomendado:
  python update_index_v2.py --root . --index planos_index.json --report-dir artifacts \
    --auto-map-smart --prefer-folder "Planos de Estudos" --auto-demote-canonical --write --allow-unresolved

"""

from __future__ import annotations
import argparse, os, sys, json, re, subprocess, unicodedata
from collections import defaultdict
from pathlib import Path
from difflib import SequenceMatcher

def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def norm_spaces(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def normalize_basename(name: str) -> str:
    s = name
    s = s.replace("\\", "/")
    s = os.path.basename(s)
    s = s.replace("°", "o")  # converte ordinal
    s = s.replace("º", "o")
    s = s.replace("ª", "a")
    s = strip_accents(s)
    s = s.lower()
    s = norm_spaces(s)
    s = s.replace(" - ", " ")
    s = s.replace("_", " ")
    s = re.sub(r'\s+', ' ', s)
    return s

def norm_path(p: str) -> str:
    p = p.replace("\\", "/")
    p = re.sub(r"/+", "/", p).strip()
    return p

def load_index(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def save_json(obj: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def list_repo_files(root: Path) -> set[str]:
    # Tenta 'git ls-files' primeiro
    try:
        out = subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True)
        files = set(norm_path(line) for line in out.splitlines() if line.strip())
        if files:
            return files
    except Exception:
        pass
    # Fallback: varre o FS
    files = set()
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            rel = norm_path(os.path.relpath(os.path.join(dirpath, fn), root))
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

def choose_canonical(candidates: list[dict], prefer_folder: str|None, repo_files: set[str]) -> int:
    def score(d):
        p = d["path"]
        ok = 1 if p in repo_files else 0
        pref = 1 if (prefer_folder and prefer_folder.lower() in p.lower()) else 0
        return (ok, pref)
    best_i, best_s = 0, (-1, -1)
    for i, d in enumerate(candidates):
        s = score(d)
        if s > best_s:
            best_i, best_s = i, s
    return best_i

def apply_rename_table(path: str, table: dict[str,str]) -> str:
    p = path
    for old, new in table.items():
        p = p.replace(old, new)
    return p

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="Raiz do repositório")
    ap.add_argument("--index", default="planos_index.json", help="Caminho do arquivo de índice")
    ap.add_argument("--write", action="store_true", help="Escreve as alterações no arquivo de índice")
    ap.add_argument("--allow-unresolved", action="store_true", help="Não falha mesmo com pendências")
    ap.add_argument("--prefer-folder", default="Planos de Estudos", help="Pasta preferida para canonicidade")
    ap.add_argument("--auto-demote-canonical", action="store_true", help="Desmarca canonical em conflitos")
    ap.add_argument("--report-dir", default="artifacts", help="Diretório para relatórios")
    ap.add_argument("--auto-map-smart", action="store_true", help="Ativa remapeamento inteligente por nome normalizado e heurísticas de pasta")
    ap.add_argument("--use-similarity-fallback", action="store_true", help="Permite escolher melhor candidato por similaridade quando houver múltiplos")
    ap.add_argument("--rename-table", default="", help="JSON de mapeamento simples de renome de diretórios (substituições)")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    index_path = (root / args.index).resolve()
    report_dir = (root / args.report_dir).resolve()

    idx = load_index(index_path)
    docs = flatten_docs(idx)
    repo_files = list_repo_files(root)

    rename_table = {}
    if args.rename_table:
        try:
            rename_table = json.loads(Path(args.rename_table).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Aviso: não foi possível carregar rename-table: {e}", file=sys.stderr)

    # Auditoria de paths
    missing = []
    present = []
    for d in docs:
        if d["path"] in repo_files:
            present.append(d["path"])
        else:
            missing.append(d["path"])

    changed_paths = []

    # Remap básico por nome exato (da v1)
    by_name = defaultdict(list)
    for f in repo_files:
        by_name[os.path.basename(f).lower()].append(f)

    remap_exact = {}
    for d in docs:
        p = d["path"]
        if p in repo_files:
            continue
        fname = os.path.basename(p).lower()
        cands = by_name.get(fname, [])
        if len(cands) == 1:
            remap_exact[p] = cands[0]

    # Aplicar remap exato
    for col in idx.get("collections", []):
        for d in col.get("docs", []):
            p = norm_path(d.get("path",""))
            if p in remap_exact:
                d["path"] = remap_exact[p]
                changed_paths.append({"method":"exact-name","from": p, "to": remap_exact[p]})

    # SMART remap
    if args.auto_map_smart:
        # Recalcular missing
        docs2 = flatten_docs(idx)
        still_missing_docs = [d for d in docs2 if d["path"] not in repo_files]

        # Índice por nome normalizado
        pool = list(repo_files)
        smart_index = defaultdict(list)
        for f in pool:
            smart_index[normalize_basename(os.path.basename(f))].append(f)

        for d in still_missing_docs:
            old = d["path"]
            # 1) rename-table substituições
            candidate_after_rename = apply_rename_table(old, rename_table) if rename_table else old
            if candidate_after_rename in repo_files:
                d["_doc"]["path"] = candidate_after_rename
                changed_paths.append({"method":"rename-table","from": old, "to": candidate_after_rename})
                continue

            # 2) match por nome normalizado
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
                    # bônus se diretório base coincide (e.g., "Planos de Aula/Games")
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

    # Recomputar missing depois do smart
    docs3 = flatten_docs(idx)
    still_missing = [d["path"] for d in docs3 if d["path"] not in repo_files]

    # Conflitos de canonicidade
    groups = defaultdict(list)
    for d in docs3:
        key = (d["area"], d["grades"], d["year"], d["semester"], d["type"])
        if d["canonical"]:
            groups[key].append(d)

    canonical_conflicts = {k:v for k,v in groups.items() if len(v) > 1}
    canonical_resolved = []

    if canonical_conflicts and args.auto_demote_canonical:
        for key, lst in canonical_conflicts.items():
            keep_i = choose_canonical(lst, args.prefer_folder, repo_files)
            for i, d in enumerate(lst):
                if i == keep_i:
                    continue
                d["_doc"]["canonical"] = False
                canonical_resolved.append({
                    "key": list(key),
                    "demoted_path": d["path"]
                })
        # Recalcula conflitos restantes
        groups2 = defaultdict(list)
        docs4 = flatten_docs(idx)
        for d in docs4:
            key = (d["area"], d["grades"], d["year"], d["semester"], d["type"])
            if d["canonical"]:
                groups2[key].append(d)
        canonical_conflicts = {k:v for k,v in groups2.items() if len(v) > 1}

    # Relatórios
    report = {
        "summary": {
            "total_docs": len(docs),
            "paths_present": len([p for p in docs if p["path"] in repo_files]),
            "paths_missing_before": len(missing),
            "paths_auto_remapped": len([c for c in changed_paths if c["method"] in ("exact-name","smart-name","rename-table")]),
            "paths_missing_after": len(still_missing),
            "canonical_conflicts": len(canonical_conflicts),
            "canonical_auto_demoted": len(canonical_resolved)
        },
        "changed_paths": changed_paths,
        "still_missing": still_missing,
        "canonical_conflicts_detail": {
            str(k): [d["path"] for d in v] for k, v in canonical_conflicts.items()
        },
        "canonical_auto_demoted": canonical_resolved
    }

    report_dir.mkdir(parents=True, exist_ok=True)
    with (report_dir / "update_index_report.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    md = ["# Relatório de Auditoria do planos_index.json",
          "",
          "## Resumo",
          f"- Total de documentos no index: **{report['summary']['total_docs']}**",
          f"- Paths válidos: **{report['summary']['paths_present']}**",
          f"- Paths faltando (antes do remap): **{report['summary']['paths_missing_before']}**",
          f"- Paths realocados automaticamente: **{report['summary']['paths_auto_remapped']}**",
          f"- Paths faltando (depois do remap): **{report['summary']['paths_missing_after']}**",
          f"- Conflitos de canonicidade: **{report['summary']['canonical_conflicts']}**",
          f"- Canonicals desmarcados automaticamente: **{report['summary']['canonical_auto_demoted']}**",
          "",
          "## Paths alterados"]
    if report["changed_paths"]:
        for ch in report["changed_paths"]:
            md.append(f"- ({ch['method']}) `{ch['from']}` → `{ch['to']}`")
    else:
        md.append("- (nenhum)")

    md.append("\n## Paths ainda faltando")
    if still_missing:
        for p in still_missing:
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

    # Grava o index, se solicitado
    if args.write:
        save_json(idx, index_path)

    # Status de saída
    if report["summary"]["paths_missing_after"] > 0 and not args.allow_unresolved:
        print("Há paths pendentes. Revise artifacts/update_index_report.md", file=sys.stderr)
        sys.exit(2)
    if report["summary"]["canonical_conflicts"] > 0 and not args.allow_unresolved:
        print("Há conflitos de canonicidade. Revise artifacts/update_index_report.md", file=sys.stderr)
        sys.exit(3)

    print("OK")

if __name__ == "__main__":
    main()
