"""Create an English 600 dpi correlation profile figure without significance claims."""
from __future__ import annotations
import csv,os
from pathlib import Path
os.environ.setdefault('MPLBACKEND','Agg');os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parents[1]/'logs'/'matplotlib_config'))
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]; IN=ROOT/'results'/'exploratory'/'broad_event_profile_correlations_v1.csv'; OUT=ROOT/'submission_figures'/'Figure_4_exploratory_correlation_profile.png'
def main():
 if OUT.exists():raise FileExistsError('Refusing overwrite')
 with IN.open(encoding='utf-8-sig',newline='') as h:r=list(csv.DictReader(h))
 chars=[]
 for x in r:
  if x['characteristic'] not in chars:chars.append(x['characteristic'])
 outcomes=[]
 for x in r:
  if x['outcome'] not in outcomes:outcomes.append(x['outcome'])
 a=np.array([[float(next(x['spearman_rho'] for x in r if x['outcome']==o and x['characteristic']==c)) for c in chars] for o in outcomes])
 labels={'log_geometry_area_ha':'Estimated geometry area','log1p_detection_interval_hours':'Detection interval','temperature_mean_first_24h_c':'Mean temperature','temperature_max_first_24h_c':'Maximum temperature','vpd_mean_first_24h_kpa':'Mean VPD','vpd_p95_first_24h_kpa':'VPD 95th percentile','wind_mean_first_24h_ms':'Mean wind','wind_p95_first_24h_ms':'Wind 95th percentile','anchor_elevation_m':'Elevation','local_day':'Local day of month','local_hour':'Local hour','anchor_latitude':'Latitude','anchor_longitude':'Longitude'}
 fig,ax=plt.subplots(figsize=(12,3.8));im=ax.imshow(a,cmap='coolwarm',vmin=-1,vmax=1);ax.set(xticks=np.arange(len(chars)),xticklabels=[labels[x] for x in chars],yticks=np.arange(len(outcomes)),yticklabels=[labels[x] for x in outcomes],title='Exploratory Spearman correlation profile')
 plt.setp(ax.get_xticklabels(),rotation=45,ha='right');
 for i in range(a.shape[0]):
  for j in range(a.shape[1]):ax.text(j,i,f'{a[i,j]:.2f}',ha='center',va='center',fontsize=8)
 cb=fig.colorbar(im,ax=ax);cb.set_label('Spearman rho');fig.tight_layout();fig.savefig(OUT,dpi=600,bbox_inches='tight');plt.close(fig)
 if min(Image.open(OUT).info.get('dpi',(0,0)))<599:raise RuntimeError('DPI check failed')
if __name__=='__main__':main()
