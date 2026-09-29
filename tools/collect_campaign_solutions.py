#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, json, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.submission_format import SubmissionError, parse_block  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def choose_block(run_dir: Path) -> tuple[Path, str]:
    final = run_dir / 'submission_block.txt'
    cached = run_dir / 'best_cache_exact_submission_block.txt'
    if final.is_file() and final.stat().st_size:
        return final, 'submission_block'
    if cached.is_file() and cached.stat().st_size:
        return cached, 'best_cache_exact_submission_block'
    raise SubmissionError(f'{run_dir}: no persisted submission block found')


def main() -> int:
    ap = argparse.ArgumentParser(description='Recover the best persisted 3-line block from every campaign cell.')
    ap.add_argument('--campaign', required=True, type=Path)
    ap.add_argument('--output-dir', required=True, type=Path)
    args = ap.parse_args()
    campaign = args.campaign.resolve()
    runs = campaign / 'runs'
    if not runs.is_dir():
        raise SubmissionError(f'campaign has no runs directory: {runs}')

    records = []
    by_pair = {}
    errors = []
    for run_dir in sorted(p for p in runs.iterdir() if p.is_dir()):
        try:
            path, kind = choose_block(run_dir)
            tau, k, lines = parse_block(path)
            key = (tau, k)
            if key in by_pair:
                raise SubmissionError(f'duplicate parameter pair {key}: {path} and {by_pair[key][0]}')
            verified_marker = run_dir / 'VERIFIED_OK.tsv'
            verified = verified_marker.is_file() and verified_marker.stat().st_size > 0
            claim_slack = run_dir / 'submission_claim_slack.tsv'
            record = {
                'tag': run_dir.name, 'tau': tau, 'k': k, 'source': str(path),
                'source_kind': kind, 'sha256': sha256(path), 'cgal_verified_marker_present': verified,
                'claim_slack_source': str(claim_slack) if claim_slack.is_file() else None,
            }
            records.append((tau, k, lines, record))
            by_pair[key] = (path, record)
        except Exception as exc:  # noqa: BLE001
            errors.append(f'{run_dir.name}: {exc}')

    if errors:
        raise SubmissionError('failed to recover all campaign cells:\n  ' + '\n  '.join(errors))
    if len(records) != 9:
        raise SubmissionError(f'expected exactly 9 recovered cells, found {len(records)}')
    taus = sorted({tau for tau, _, _, _ in records})
    ks = sorted({k for _, k, _, _ in records})
    expected = {(tau, k) for tau in taus for k in ks}
    if len(taus) != 3 or len(ks) != 3 or set(by_pair) != expected:
        raise SubmissionError('recovered blocks are not a complete 3 tau x 3 k grid')

    out = args.output_dir.resolve()
    if out.exists(): shutil.rmtree(out)
    (out / 'blocks').mkdir(parents=True)
    (out / 'claim_slack').mkdir(parents=True)
    ordered = []
    manifest = []
    for tau, k, lines, rec in sorted(records, key=lambda x: (x[0], x[1])):
        text = '\n'.join(lines) + '\n'
        name = f'tau_{tau:.17g}_k_{k}.txt'
        (out / 'blocks' / name).write_text(text, encoding='utf-8', newline='\n')
        ordered.extend(lines)
        rec = dict(rec)
        rec['archive_block'] = f'blocks/{name}'
        source_audit = rec.get('claim_slack_source')
        if source_audit:
            audit_name = f'{rec["tag"]}.tsv'
            shutil.copy2(source_audit, out / 'claim_slack' / audit_name)
            rec['archive_claim_slack'] = f'claim_slack/{audit_name}'
        manifest.append(rec)
    (out / 'solutions.txt').write_text('\n'.join(ordered) + '\n', encoding='utf-8', newline='\n')
    payload = {
        'schema': 'ringarc31.recovered-campaign-solutions.v1',
        'campaign': str(campaign), 'taus': taus, 'ks': ks,
        'cells': manifest,
        'note': 'submission_block.txt is preferred; best_cache_exact_submission_block.txt is used only as a recovery fallback.',
    }
    (out / 'solution_manifest.json').write_text(json.dumps(payload, indent=2, sort_keys=True) + '\n')
    print(out)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SubmissionError as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        raise SystemExit(2)
