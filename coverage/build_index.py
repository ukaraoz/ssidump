"""Write coverage/index.html: one row per dataset whose pages have been built.

    python3 coverage/build_index.py

Reads the data embedded in each built <dataset>_coverage.html (in coverage/ and
geochem_coverage/), so run it after coverage/build.sh.
"""
import html, json, re
from pathlib import Path

from build_data import DATASETS

root = Path(__file__).resolve().parent
repo = root.parent
rows = []
for key, cfg in DATASETS.items():
    folder = 'geochem_coverage' if key == 'geochem' else 'coverage'
    page = repo / folder / f'{key}_coverage.html'
    if not page.exists():
        continue
    s = page.read_text(encoding='utf-8')
    D = json.loads(re.search(r'const D = (\{.*?\});\n', s, re.S).group(1).replace('<\\/', '</'))
    prefix = '' if folder == 'coverage' else '../geochem_coverage/'
    rows.append(dict(key=key, label=cfg['label'], href=f'{prefix}{key}_coverage.html',
                     yhref=f'{prefix}{key}_coverage_by_year.html', total=D['total'],
                     sites=len({r[0] for r in D['rows']}), cols=len(D['cols']), colword=cfg['cols'],
                     span=f"{D['span'][0][:4]}–{D['span'][1][:4]}",
                     sel=cfg.get('select') or f"--family {cfg['family']}"))
rows.sort(key=lambda r: -r['total'])

tr = '\n'.join(
    f'<tr><th scope="row"><a href="{r["href"]}">{html.escape(r["label"])}</a></th>'
    f'<td class="n">{r["total"]:,}</td><td class="n">{r["sites"]}</td>'
    f'<td class="n">{r["cols"]} <span class="u">{html.escape(r["colword"] if r["cols"] != 1 else r["colword"].rstrip("s"))}</span></td>'
    f'<td class="n">{r["span"]}</td>'
    f'<td><a href="{r["href"]}">All years</a> · <a href="{r["yhref"]}">By year</a></td>'
    f'<td><code>{html.escape(r["sel"])}</code></td></tr>' for r in rows)
total = sum(r['total'] for r in rows)

out = f'''<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>East River Data Coverage</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: header, then one table of datasets linking to their heatmap pages. */
:root{{--bg:#f6f7f9;--surface:#fff;--line:#dde1e7;--ink:#1b2129;--ink-2:#4a5463;--ink-3:#7a8494;--accent:#256abf;
  --f-ui:"Public Sans",system-ui,-apple-system,"Segoe UI",sans-serif;--f-data:"IBM Plex Mono",ui-monospace,Menlo,monospace}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{--bg:#12161b;--surface:#191e25;--line:#2c333d;--ink:#e8ebf0;--ink-2:#aab3c0;--ink-3:#7c8695;--accent:#6da7ec;color-scheme:dark}}}}
:root[data-theme="dark"]{{--bg:#12161b;--surface:#191e25;--line:#2c333d;--ink:#e8ebf0;--ink-2:#aab3c0;--ink-3:#7c8695;--accent:#6da7ec;color-scheme:dark}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 var(--f-ui)}}
.wrap{{max-width:1180px;margin:0 auto;padding-block:28px 40px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px));display:flex;flex-direction:column;gap:18px}}
.eyebrow{{font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);font-weight:600}}
h1{{font-size:26px;line-height:1.15;margin:4px 0 6px;letter-spacing:-.01em;text-wrap:balance}}
p{{margin:0;color:var(--ink-2);max-width:72ch}}
a{{color:var(--accent)}}
.frame{{background:var(--surface);border:1px solid var(--line);border-radius:8px;overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}
th,td{{padding:9px 14px;text-align:left;border-top:1px solid var(--line);white-space:nowrap;vertical-align:baseline}}
thead th{{border-top:0;font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--ink-3);font-weight:600}}
tbody th{{font-weight:600}}
td.n{{text-align:right;font-family:var(--f-data);font-size:12.5px}}
thead th.n{{text-align:right}}
.u{{font-family:var(--f-ui);color:var(--ink-3);font-size:12px}}
code{{font-family:var(--f-data);font-size:11.5px;color:var(--ink-2)}}
tfoot td,tfoot th{{font-weight:600;border-top:2px solid var(--line)}}
.foot{{font-size:12px;color:var(--ink-3)}}
</style>
<div class="wrap">
  <header>
    <div class="eyebrow">East River · SSI datastreams</div>
    <h1>Data coverage by source</h1>
    <p>Heatmaps of data-point counts per site, depth, year and variable for each data source exported from the SSI
    <code>odmx_lbnlsfa</code> database. Open a source for its all-years page or its year-by-year page.</p>
  </header>
  <div class="frame"><table>
    <thead><tr><th>Source</th><th class="n">Data points</th><th class="n">Sites</th><th class="n">Variables</th><th class="n">Years</th><th>Pages</th><th>Export selection</th></tr></thead>
    <tbody>
{tr}
    </tbody>
    <tfoot><tr><th>All sources</th><td class="n">{total:,}</td><td></td><td></td><td></td><td></td><td></td></tr></tfoot>
  </table></div>
  <p class="foot">Built from <code>ssiexport.py export datastreams … --drop-nan</code> exports (geochem without <code>--drop-nan</code>) of the 2026-10-26 dump. Sentinel satellite streams are not included. Schema map: <a href="../schema_map/ssi_schemas.html">ssi_schemas.html</a>.</p>
</div>
'''
(root / 'index.html').write_text(out, encoding='utf-8')
print(f'wrote {root / "index.html"} ({len(rows)} datasets)')
