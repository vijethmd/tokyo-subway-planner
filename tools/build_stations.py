#!/usr/bin/env python3
"""Build the station dataset embedded in TokyoSubwayPlanner.html.

Downloads Tokyo Metro + Toei subway route relations from OpenStreetMap
(Overpass API), merges same-named stops into stations, assigns station
codes and calibrates each line's distances to its official length.

    python3 tools/build_stations.py                    # download, rewrite the data block in place
    python3 tools/build_stations.py --osm saved.json   # reuse a saved Overpass download
"""
import json, math, os, re, sys, urllib.parse, urllib.request

QUERY = """[out:json][timeout:180];
relation["route"="subway"]["network"~"東京メトロ|都営地下鉄"](35.45,139.45,35.95,140.1)->.r;
.r out body; node(r.r); out body;"""

# One relation per line, in station-code order. Chiyoda's Kita-ayase stub is
# appended to the main line; the Oedo relation runs Tochomae → ring → Tochomae → Hikarigaoka.
LINES = [
  # id, relations, name, Japanese, colour, operator, official km,
  # through-running onto other railways [towards first station, towards last station]
  ('G', [8026074], 'Ginza', '銀座線', '#FF9500', 'metro', 14.2, [0, 0]),
  ('M', [443282], 'Marunouchi', '丸ノ内線', '#F62E36', 'metro', 24.2, [0, 0]),
  ('Mb', [8015930], 'Marunouchi', '丸ノ内線', '#F62E36', 'metro', 3.2, [0, 0]),
  ('H', [443272], 'Hibiya', '日比谷線', '#B5B5AC', 'metro', 20.3, [0, 1]),
  ('T', [5371620], 'Tozai', '東西線', '#009BBF', 'metro', 30.8, [1, 1]),
  ('C', [443284, 8026053], 'Chiyoda', '千代田線', '#00BB85', 'metro', 24.0, [1, 1]),
  ('Y', [443269], 'Yurakucho', '有楽町線', '#C1A470', 'metro', 28.3, [1, 0]),
  ('Z', [443279], 'Hanzomon', '半蔵門線', '#8F76D6', 'metro', 16.8, [1, 1]),
  ('N', [7730199], 'Namboku', '南北線', '#00AC9B', 'metro', 21.3, [1, 1]),
  ('F', [5375678], 'Fukutoshin', '副都心線', '#9C5E31', 'metro', 20.2, [1, 1]),
  ('A', [3302734], 'Asakusa', '浅草線', '#E85298', 'toei', 18.3, [1, 1]),
  ('I', [443286], 'Mita', '三田線', '#0079C2', 'toei', 26.5, [1, 0]),
  ('S', [8019858], 'Shinjuku', '新宿線', '#6CBB5A', 'toei', 23.5, [1, 0]),
  ('E', [8019883], 'Oedo', '大江戸線', '#B6007A', 'toei', 40.7, [0, 0]),
]

# Romanisation fixes, in Tokyo Metro / Toei house style (no macrons).
EN = {
  '茅場町': 'Kayabacho', '木場': 'Kiba', '新高島平': 'Shin-takashimadaira', '池袋': 'Ikebukuro',
  '明治神宮前': 'Meiji-jingumae (Harajuku)', '押上': 'Oshiage (Skytree)', '二重橋前': 'Nijubashimae',
  '新宿': 'Shinjuku', '馬喰横山': 'Bakuro-yokoyama', '板橋本町': 'Itabashi-honcho', '人形町': 'Ningyocho',
  '広尾': 'Hiro-o', '目黒': 'Meguro', '高田馬場': 'Takadanobaba', '神楽坂': 'Kagurazaka', '日本橋': 'Nihombashi',
  '西船橋': 'Nishi-funabashi', '霞ケ関': 'Kasumigaseki', '永田町': 'Nagatacho', '有楽町': 'Yurakucho',
  '三越前': 'Mitsukoshimae', '清澄白河': 'Kiyosumi-shirakawa', '住吉': 'Sumiyoshi', '東大前': 'Todaimae',
  '本駒込': 'Hon-komagome', '王子神谷': 'Oji-kamiya', '志茂': 'Shimo', '日比谷': 'Hibiya', '東新宿': 'Higashi-shinjuku',
  '飯田橋': 'Iidabashi', '森下': 'Morishita', '小竹向原': 'Kotake-mukaihara', '葛西': 'Kasai', '西葛西': 'Nishi-kasai',
  '市ケ谷': 'Ichigaya', '西ケ原': 'Nishigahara', '築地市場': 'Tsukijishijo', '中延': 'Nakanobu',
}

# Different-name stations with an official transfer route between them.
LINKS = [('赤坂見附', '永田町'), ('溜池山王', '国会議事堂前'), ('日比谷', '有楽町'), ('銀座', '銀座一丁目'),
  ('後楽園', '春日'), ('淡路町', '新御茶ノ水'), ('淡路町', '小川町'), ('新御茶ノ水', '小川町'),
  ('東日本橋', '馬喰横山'), ('人形町', '水天宮前'), ('上野広小路', '仲御徒町'), ('上野広小路', '上野御徒町'),
  ('仲御徒町', '上野御徒町'), ('新橋', '汐留'), ('新宿', '新宿西口'), ('虎ノ門', '虎ノ門ヒルズ')]

# Labelled on the network map even when zoomed out.
MAJOR = {'東京', '新宿', '渋谷', '池袋', '上野', '銀座', '浅草', '六本木'}

def norm_ja(n):
  n = re.sub(r'〈.*?〉', '', n).strip()
  n = n.removesuffix('駅').replace('ヶ', 'ケ')
  return {'新線新宿': '新宿'}.get(n, n)

def style_en(n):
  # "Kita-Senju" → "Kita-senju": capital first letter only, as on station signs.
  n = re.sub(r"\s*'.*?'", '', n).replace('ō', 'o').replace('Ō', 'O').replace('ū', 'u')
  return '-'.join([p if i == 0 else p[:1].lower() + p[1:] for i, p in enumerate(n.split('-'))])

def hav(a, b):
  R = 6371; la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
  x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
  return 2 * R * math.asin(math.sqrt(x))

def slug(en):
  return re.sub(r'[^a-z0-9]+', '_', re.sub(r'\(.*?\)', '', en).lower()).strip('_')

def main():
  if '--osm' in sys.argv:
    data = json.load(open(sys.argv[sys.argv.index('--osm') + 1]))
  else:
    req = urllib.request.Request('https://overpass-api.de/api/interpreter',
      data=urllib.parse.urlencode({'data': QUERY}).encode(), headers={'User-Agent': 'tokyo-subway-planner'})
    data = json.load(urllib.request.urlopen(req, timeout=240))
  nodes = {e['id']: e for e in data['elements'] if e['type'] == 'node'}
  rels = {e['id']: e for e in data['elements'] if e['type'] == 'relation'}

  stations = {}  # normalised Japanese name -> station
  lines_out = []
  for lid, rids, name, ja, colour, op, km, through in LINES:
    stops = []
    for rid in rids:
      for m in rels[rid]['members']:
        if m['type'] == 'node' and m['role'].startswith('stop'):
          n = nodes[m['ref']]; t = n.get('tags', {})
          j = norm_ja(t['name'])
          if stops and stops[-1][0] == j: continue  # Ayase repeats where the stub joins
          stops.append((j, t.get('name:en'), round(n['lat'], 5), round(n['lon'], 5)))
    if lid == 'E':
      # Relation: Tochomae(E28), E01..E27, Tochomae(E28), E29..E38. Store it as one
      # path Hikarigaoka → Tochomae → E01 … E27 → Tochomae: ring trains terminate at
      # Tochomae, so changing there is a real change of train.
      tail = stops[28:]  # Tochomae, E29..E38
      stops = list(reversed(tail)) + stops[1:28] + [stops[0]]
      codes = [f'E{n:02d}' for n in range(38, 28, -1)] + ['E28'] + [f'E{n:02d}' for n in range(1, 28)] + ['E28']
    elif lid == 'Mb':
      codes = ['M06', 'Mb05', 'Mb04', 'Mb03']
    else:
      codes = [f'{lid}{i + 1:02d}' for i in range(len(stops))]
    seq = []
    for (j, en, lat, lng), code in zip(stops, codes):
      s = stations.setdefault(j, {'ja': j, 'en': None, 'codes': {}, 'p': {}})
      cand = EN.get(j) or (style_en(en) if en else None)
      if cand and not s['en']: s['en'] = cand
      s['codes'].setdefault(lid, code)
      s['p'].setdefault(lid, [lat, lng])
      seq.append(j)
    raw = sum(hav(stations[a]['p'][lid], stations[b]['p'][lid]) for a, b in zip(seq, seq[1:]))
    lines_out.append({'id': lid, 'name': name, 'ja': ja, 'color': colour, 'op': op, 'through': through,
                      'k': round(km / raw, 3), 'seq': seq})
    print(f'// {lid}: {len(seq)} stops, {raw:.1f} km straight-line vs {km} km official', file=sys.stderr)

  ids = {}
  for j, s in stations.items():
    assert s['en'], f'no English name for {j}'
    sid = slug(s['en'])
    assert sid not in ids, sid
    ids[sid] = j; s['id'] = sid
    pts = list(s['p'].values())
    s['lat'] = round(sum(p[0] for p in pts) / len(pts), 5); s['lng'] = round(sum(p[1] for p in pts) / len(pts), 5)
    spread = max(hav(a, b) for a in pts for b in pts)
    if spread > 0.35: print(f'// wide station {s["en"]}: {spread * 1000:.0f} m across lines', file=sys.stderr)
  ends = {l['seq'][0] for l in lines_out if l['id'] not in ('Mb',)} | {l['seq'][-1] for l in lines_out}
  for l in lines_out: l['seq'] = [stations[j]['id'] for j in l['seq']]
  for a, b in LINKS: assert a in stations and b in stations, (a, b)

  out = []
  out.append('  lines:[')
  for l in lines_out:
    out.append('    ' + json.dumps(l, ensure_ascii=False, separators=(',', ':')) + ',')
  out.append('  ],')
  out.append('  // Different-name stations joined by an official transfer route.')
  out.append('  links:' + json.dumps([[stations[a]['id'], stations[b]['id']] for a, b in LINKS], separators=(',', ':')) + ',')
  out.append('  stations:[')
  for s in sorted(stations.values(), key=lambda s: s['en']):
    rec = {'id': s['id'], 'name': s['en'], 'ja': s['ja'], 'lat': s['lat'], 'lng': s['lng'], 'codes': s['codes'], 'p': s['p']}
    if s['ja'] in MAJOR: rec['major'] = 1
    if s['ja'] in ends: rec['terminal'] = 1
    out.append('    ' + json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + ',')
  out.append('  ]')
  html = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'TokyoSubwayPlanner.html')
  src = open(html, encoding='utf-8').read()
  begin, end = '  // BEGIN GENERATED by tools/build_stations.py\n', '  // END GENERATED\n'
  a, b = src.index(begin) + len(begin), src.index(end)
  open(html, 'w', encoding='utf-8').write(src[:a] + '\n'.join(out) + '\n' + src[b:])
  print(f'// {len(stations)} stations written to {html}', file=sys.stderr)

main()
