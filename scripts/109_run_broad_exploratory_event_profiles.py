"""Describe broad noncausal profiles of verified satellite detected events."""
from __future__ import annotations
import csv,hashlib,json
from datetime import datetime
from pathlib import Path
import numpy as np
from scipy.stats import spearmanr
from statsmodels.stats.multitest import multipletests
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'/'processed'/'gwis_event_analysis_v1.csv'; OUT=ROOT/'results'/'exploratory'/'broad_event_profile_correlations_v1.csv'; GROUP=ROOT/'results'/'exploratory'/'broad_event_profile_tertiles_v1.csv'; AUDIT=ROOT/'results'/'exploratory'/'audit_broad_event_profiles_v1.json'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest().upper()
def main():
 if any(p.exists() for p in [OUT,GROUP,AUDIT]):raise FileExistsError('Refusing overwrite')
 with DATA.open(encoding='utf-8-sig',newline='') as h: rows=[r for r in csv.DictReader(h) if r['primary_cohort'].lower()=='true']
 for r in rows:
  t=datetime.fromisoformat(r['gwis_initial_datetime_colombia']);r['local_day']=t.day;r['local_hour']=t.hour+r['anchor_lag_minutes_after_gwis_initial'] if 'anchor_lag_minutes_after_gwis_initial' in r else t.hour
 # Exact temporal fields are intentionally limited to calendar day and initial local hour.
 characteristics=['temperature_mean_first_24h_c','temperature_max_first_24h_c','vpd_mean_first_24h_kpa','vpd_p95_first_24h_kpa','wind_mean_first_24h_ms','wind_p95_first_24h_ms','anchor_elevation_m','local_day','local_hour','anchor_latitude','anchor_longitude']
 outcomes=['log_geometry_area_ha','log1p_detection_interval_hours']
 rec=[]
 for outcome in outcomes:
  for char in characteristics:
   rho,p=spearmanr([float(r[outcome]) for r in rows],[float(r[char]) for r in rows])
   rec.append({'outcome':outcome,'characteristic':char,'spearman_rho':rho,'p_value_two_sided':p,'bh_q_value':'','n':len(rows),'analysis_class':'exploratory descriptive association'})
 _,q,_,_=multipletests([r['p_value_two_sided'] for r in rec],method='fdr_bh')
 for r,x in zip(rec,q,strict=True):r['bh_q_value']=x
 with OUT.open('x',encoding='utf-8-sig',newline='') as h:w=csv.DictWriter(h,fieldnames=list(rec[0]));w.writeheader();w.writerows(rec)
 groups=[]
 for outcome,label in [('log_geometry_area_ha','estimated_geometry_size_tertile'),('log1p_detection_interval_hours','detection_interval_tertile')]:
  order=np.argsort(np.asarray([float(r[outcome]) for r in rows])); membership={rows[i]['fire_id']:min(3,int(rank*3/len(rows))+1) for rank,i in enumerate(order)}
  for char in characteristics:
   for tertile in (1,2,3):
    v=np.asarray([float(r[char]) for r in rows if membership[r['fire_id']]==tertile]);groups.append({'stratification':label,'characteristic':char,'tertile':tertile,'n':len(v),'median':np.median(v),'q25':np.quantile(v,.25),'q75':np.quantile(v,.75)})
 with GROUP.open('x',encoding='utf-8-sig',newline='') as h:w=csv.DictWriter(h,fieldnames=list(groups[0]));w.writeheader();w.writerows(groups)
 audit={'cohort_n':len(rows),'characteristics':characteristics,'outcomes':outcomes,'association_test':'Spearman rank correlation, two sided, Benjamini Hochberg across 22 outcome characteristic pairs','significant_q_below_0_05':[{'outcome':r['outcome'],'characteristic':r['characteristic'],'rho':r['spearman_rho'],'q':r['bh_q_value']} for r in rec if r['bh_q_value']<.05],'inputs':{'file':str(DATA.relative_to(ROOT)),'sha256':sha(DATA)},'outputs':{'correlations':{'file':str(OUT.relative_to(ROOT)),'sha256':sha(OUT)},'tertiles':{'file':str(GROUP.relative_to(ROOT)),'sha256':sha(GROUP)}},'interpretation_limit':'Exploratory event profiles describe correlations among operational GWIS event attributes and sampled covariates. They do not identify causes, occurrence risk, or physical burned area.'}
 AUDIT.write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'n':len(rows),'q_below_0_05':audit['significant_q_below_0_05']},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
