"""Recover hourly ERA5 Land precipitation increments from forecast accumulations."""
from __future__ import annotations
import hashlib,json
from datetime import datetime
from pathlib import Path
import eccodes,numpy as np
ROOT=Path(__file__).resolve().parents[1]; RAW=ROOT/'data'/'raw'/'era5_land'
SOURCES=[RAW/'era5_land_core_hourly_2026-06_tolima_screening_bbox.grib',RAW/'era5_land_core_hourly_2026-07_tolima_screening_bbox.grib',RAW/'era5_land_tp_hourly_2026-08_tolima_screening_bbox.grib',RAW/'era5_land_core_2026-08-31T20-23Z_supplement_check.grib',RAW/'era5_land_core_hourly_2026-09-01_tolima_screening_bbox.grib']
OUT=ROOT/'data'/'interim'/'era5_land_hourly_precipitation_increment_v1.npz'; AUDIT=ROOT/'results'/'exploratory'/'audit_era5_land_hourly_precipitation_increment_v1.json'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest().upper()
def dt(d,t): return datetime.strptime(f'{d}{t:04d}','%Y%m%d%H%M')
def main():
 if OUT.exists() or AUDIT.exists():raise FileExistsError('Refusing overwrite')
 records={}; lat=lon=None
 for p in SOURCES:
  with p.open('rb') as h:
   while m:=eccodes.codes_grib_new_from_file(h):
    try:
     if eccodes.codes_get(m,'shortName')!='tp':continue
     valid=dt(eccodes.codes_get(m,'validityDate'),eccodes.codes_get(m,'validityTime')); ref=dt(eccodes.codes_get(m,'dataDate'),eccodes.codes_get(m,'dataTime')); rng=str(eccodes.codes_get(m,'stepRange')); a,b=map(int,rng.split('-'))
     key=(ref,b)
     if key in records:raise RuntimeError(f'Duplicate forecast step {key}')
     val=np.asarray(eccodes.codes_get_array(m,'values'),dtype=np.float32); records[key]=(valid,a,b,val)
     x=np.asarray(eccodes.codes_get_array(m,'latitudes'));y=np.asarray(eccodes.codes_get_array(m,'longitudes'))
     if lat is None:lat,lon=x,y
     elif not(np.array_equal(x,lat) and np.array_equal(y,lon)):raise RuntimeError('Grid mismatch')
    finally:eccodes.codes_release(m)
 increments={}; negative=0
 for (ref,end),(valid,start,_,value) in records.items():
  if start and (ref,start) not in records:
   continue
  inc=value if start==0 else value-records[(ref,start)][3]
  negative+=int((inc < -1e-8).sum())
  if valid in increments:raise RuntimeError(f'Duplicate validity {valid}')
  increments[valid]=inc
 times=sorted(increments); expected=[datetime(2026,6,1,1)+__import__('datetime').timedelta(hours=i) for i in range(2229)]
 if times!=expected or negative:raise RuntimeError(f'Coverage or negative increment failure {len(times)} {negative}')
 arr=np.stack([increments[t] for t in times]);np.savez_compressed(OUT,validity_utc=np.asarray([t.strftime('%Y-%m-%dT%H:%M:%SZ') for t in times]),latitudes=lat,longitudes=lon,tp_increment_m=arr)
 audit={'hour_count':len(times),'source_files':[{ 'file':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in SOURCES],'derivation':'For each GRIB accumulation, subtract the same forecast reference value at the preceding step. Use the raw value only for step 0 to 1.','negative_increment_count':negative,'minimum_m':float(arr.min()),'maximum_m':float(arr.max()),'output':{'file':str(OUT.relative_to(ROOT)),'sha256':sha(OUT)}}
 AUDIT.write_text(json.dumps(audit,indent=2)+'\n');print(json.dumps(audit,indent=2))
if __name__=='__main__':main()
