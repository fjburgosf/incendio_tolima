"""Describe monthly agreement between complete IDEAM gauges and nearest ERA5 cells."""
from __future__ import annotations
import csv, hashlib, json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
RAW=ROOT/'data'/'raw'/'ideam'; LINK=ROOT/'data'/'interim'/'ideam_precipitation_station_era5_linkage.csv'; COVERAGE=ROOT/'data'/'interim'/'ideam_precipitation_station_recent_coverage.csv'; GRID=ROOT/'data'/'interim'/'era5_land_hourly_grid_v1.npz'
OUTPUT=ROOT/'results'/'exploratory'/'era5_ideam_monthly_precipitation_comparison_v1.csv'; AUDIT=ROOT/'results'/'exploratory'/'audit_era5_ideam_monthly_precipitation_comparison_v1.json'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest().upper()
def main():
 if OUTPUT.exists() or AUDIT.exists(): raise FileExistsError('Refusing to overwrite')
 with COVERAGE.open(encoding='utf-8-sig',newline='') as h: complete={r['station_code'] for r in csv.DictReader(h) if r['june_to_august_2026_coverage_percent']=='100.0'}
 with LINK.open(encoding='utf-8-sig',newline='') as h: stations=[r for r in csv.DictReader(h) if r['station_code'] in complete]
 if len(stations)!=3: raise RuntimeError(f'Expected three complete stations, found {len(stations)}')
 values={r['station_code']:{} for r in stations}
 for p in RAW.glob('Precipitacion*.csv'):
  with p.open(encoding='utf-8-sig',newline='') as h:
   for r in csv.DictReader(h):
    code=r['CodigoEstacion'].strip(); day=r['Fecha'][:10]
    if code in values and '2026-06-01'<=day<='2026-08-31':
     if day in values[code]: raise RuntimeError(f'Duplicate daily gauge record {code} {day}')
     values[code][day]=float(r['Valor'].replace(',','.'))
 expected=[(datetime(2026,6,1)+timedelta(days=i)).strftime('%Y-%m-%d') for i in range(92)]
 for code,series in values.items():
  if set(series)!=set(expected): raise RuntimeError(f'Incomplete gauge series {code}')
 z=np.load(GRID,allow_pickle=False); lat=z['latitudes']; lon=z['longitudes']; tp=z['tp']; ts=[datetime.strptime(str(v),'%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc) for v in z['validity_utc']]
 rows=[]
 for s in stations:
  code=s['station_code']; d=(lon-float(s['nearest_global_grid_longitude']))**2+(lat-float(s['nearest_global_grid_latitude']))**2; ix=int(np.argmin(d))
  for month in (6,7,8):
   ideam=sum(v for day,v in values[code].items() if int(day[5:7])==month)
   start=datetime(2026,month,1,5,tzinfo=timezone.utc); end=datetime(2026,month+1,1,5,tzinfo=timezone.utc) if month<12 else None
   era=float(sum(tp[i,ix] for i,t in enumerate(ts) if start<t<=end)*1000)
   rows.append({'station_code':code,'station_name':s['station_name'],'municipality':s['official_municipality'],'month':f'2026-{month:02d}','ideam_gauge_precipitation_mm':ideam,'era5_nearest_cell_precipitation_mm':era,'difference_era5_minus_ideam_mm':era-ideam,'relative_difference_percent':(era-ideam)/ideam*100 if ideam else ''})
 with OUTPUT.open('x',encoding='utf-8-sig',newline='') as h:
  w=csv.DictWriter(h,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
 dif=np.asarray([r['difference_era5_minus_ideam_mm'] for r in rows]); a=np.asarray([r['ideam_gauge_precipitation_mm'] for r in rows]);b=np.asarray([r['era5_nearest_cell_precipitation_mm'] for r in rows])
 audit={'station_count':len(stations),'comparison_count':len(rows),'period':'June through August 2026','station_codes':[s['station_code'] for s in stations],'mean_difference_mm':float(dif.mean()),'median_difference_mm':float(np.median(dif)),'pearson_correlation':float(np.corrcoef(a,b)[0,1]),'inputs':{'linkage':sha(LINK),'coverage':sha(COVERAGE),'era5_grid':sha(GRID)},'output':{'file':str(OUTPUT.relative_to(ROOT)),'sha256':sha(OUTPUT)},'interpretation_limit':'This is a descriptive nearest cell comparison over monthly totals. IDEAM values are point gauge observations and August values are preliminary. The audit does not calibrate ERA5, estimate its error across Tolima, or validate event specific rainfall.'}
 AUDIT.write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps(audit,ensure_ascii=False,indent=2))
if __name__=='__main__': main()
