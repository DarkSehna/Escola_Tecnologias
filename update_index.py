#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_index.py — Auditoria e atualização do planos_index.json

Uso local:
    python update_index.py --root . --index planos_index.json --write --allow-unresolved

Parâmetros principais:
  --root PATH                Raiz do repositório (padrão: .)
  --index FILE               Caminho do planos_index.json (padrão: planos_index.json)
  --write                    Escreve alterações no arquivo de índice (senão só valida e gera relatório)
  --allow-unresolved         Não retorna erro de saída quando houver pendências não resolvidas
  --prefer-folder NAME       Pasta preferida para resolver canonicidade (padrão: "Planos de Estudos")
  --auto-demote-canonical    Quando houver conflito de canonicidade, desmarca os extras automaticamente
  --report-dir PATH          Onde salvar relatórios (padrão: artifacts/)

Saídas:
  - artifacts/update_index_report.json  (resumo em JSON)
  - artifacts/update_index_report.md    (relatório legível)
  - (opcional) planos_index.json atualizado (quando --write)

Regras:
  1) Paths inexistentes: tenta realocar pelo nome de arquivo (match exato). Se houver 1 candidato, substitui.
  2) Canonicidade: agrupa por (area, grades[], year, semester, type). Se houver >1 canonical:
       - Mantém o que já existe e está na pasta preferida (se informado);
       - Caso contrário mantém o primeiro do grupo;
       - Se --auto-demote-canonical, demarca os demais como canonical=false.
  3) Não cria itens novos automaticamente. Itens extras (arquivos que surgiram no repo) são apenas listados.
"""

from __future__ import annotations
import argparse, os, sys, json, re, subprocess
from collections import defaultdict
from pathlib import Path

def norm(p: str) -> str:
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
    # Tenta usar 'git ls-files' se possível (ignora arquivos não versionados)
    try:
        out = subprocess.check_output(["git", "-C", str(root), "ls-files"], text=True)
        files = set(norm(line) for line in out.splitlines() if line.strip())
        if files:
            return files
    except Exception:
        pass
    # Fallback: varre o filesystem
    files = set()
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            rel = norm(os.path.relpath(os.path.join(dirpath, fn), root))
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
                "path": norm(d.get("path","")),
                "type": d.get("type"),
                "area": d.get("area"),
                "semester": d.get("semester"),
                "year": d.get("year"),
                "grades": tuple(d.get("grades") or []),
                "canonical": bool(d.get("canonical", False)),
            })
    return docs

def choose_canonical(candidates: list[dict], prefer_folder: str|None, repo_files: set[str]) -> int:
    """
    Retorna o índice do candidato que deve permanecer como canonical.
    Heurística:
      1) Se houver item cujo path exista E contenha a pasta preferida, escolhe-o.
      2) Senão, se houver item cujo path exista, escolhe o primeiro desses.
      3) Senão, mantém o primeiro do grupo.
    """
    def exists_and_pref(d):
        p = d["path"]
        ok = p in repo_files
        pref = (prefer_folder and prefer_folder.lower() in p.lower())
        return ok, pref

    best_i = 0
    best_tuple = (-1, -1)  # (exists, pref) como inteiros
    for i, d in enumerate(candidates):
        ok, pref = exists_and_pref(d)
        key = (1 if ok else 0, 1 if pref else 0)
        if key > best_tuple:
            best_tuple = key
            best_i = i
    return best_i

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="Raiz do repositório")
    ap.add_argument("--index", default="planos_index.json", help="Caminho do arquivo de índice")
    ap.add_argument("--write", action="store_true", help="Escreve as alterações no arquivo de índice")
    ap.add_argument("--allow-unresolved", action="store_true", help="Não falha mesmo com pendências")
    ap.add_argument("--prefer-folder", default="Planos de Estudos", help="Pasta preferida para canonicidade")
    ap.add_argument("--auto-demote-canonical", action="store_true", help="Desmarca canonical em conflitos")
    ap.add_argument("--report-dir", default="artifacts", help="Diretório para relatórios")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    index_path = (root / args.index).resolve()
    report_dir = (root / args.report_dir).resolve()

    idx = load_index(index_path)
    docs = flatten_docs(idx)
    repo_files = list_repo_files(root)

    # Auditoria de paths
    missing = []
    present = []
    for d in docs:
        if d["path"] in repo_files:
            present.append(d["path"])
        else:
            missing.append(d["path"])

    # Tentar remapear por nome de arquivo (match exato)
    remap = {}
    by_name = defaultdict(list)
    for f in repo_files:
        by_name[os.path.basename(f).lower()].append(f)

    for d in docs:
        p = d["path"]
        if p in repo_files:
            continue
        fname = os.path.basename(p).lower()
        candidates = by_name.get(fname, [])
        if len(candidates) == 1:
            remap[p] = candidates[0]

    # Aplicar remap nas estruturas originais
    changed_paths = []
    for col in idx.get("collections", []):
        for d in col.get("docs", []):
            p = norm(d.get("path",""))
            if p in remap:
                d["path"] = remap[p]
                changed_paths.append({"from": p, "to": remap[p]})

    # Recomputar missing depois do remap
    docs2 = flatten_docs(idx)
    still_missing = [d["path"] for d in docs2 if d["path"] not in repo_files]

    # Conflitos de canonicidade
    from collections import defaultdict as dd
    groups = dd(list)
    for d in docs2:
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
        groups2 = dd(list)
        docs3 = flatten_docs(idx)
        for d in docs3:
            key = (d["area"], d["grades"], d["year"], d["semester"], d["type"])
            if d["canonical"]:
                groups2[key].append(d)
        canonical_conflicts = {k:v for k,v in groups2.items() if len(v) > 1}

    # Relatórios
    report = {
        "summary": {
            "total_docs": len(docs),
            "paths_present": len(present),
            "paths_missing_before": len(missing),
            "paths_auto_remapped": len(changed_paths),
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

    # Salvar relatórios
    (report_dir).mkdir(parents=True, exist_ok=True)
    save_json(report, report_dir / "update_index_report.json")

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
          "## Paths alterados (auto-remap por nome)"]
    if changed_paths:
        for ch in changed_paths:
            md.append(f"- `{ch['from']}` → `{ch['to']}`")
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
