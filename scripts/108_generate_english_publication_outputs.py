"""Generate English labelled submission tables and figures from verified outputs."""
from __future__ import annotations
import csv,json,os
from pathlib import Path
os.environ.setdefault('MPLBACKEND','Agg')
os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parents[1]/'logs'/'matplotlib_config'))
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from shapely.geometry import shape
ROOT=Path(__file__).resolve().parents[1]; DATA=ROOT/'data'/'processed'/'gwis_event_analysis_v1.csv'; PRIMARY=ROOT/'results'/'primary'/'primary_models_v1.csv'; BOUNDARY=ROOT/'data'/'raw'/'boundaries'/'cortolima_limite_municipal_25k.geojson'; OUT=ROOT/'submission_figures'
def read(p):
 with p.open(encoding='utf-8-sig',newline='') as h:return list(csv.DictReader(h))
def f(x):return float(x)
def main():
 if OUT.exists():raise FileExistsError('Refusing to overwrite publication outputs')
 OUT.mkdir(); rows=[r for r in read(DATA) if r['primary_cohort'].lower()=='true']; primary=read(PRIMARY)
 b=json.loads(BOUNDARY.read_text(encoding='utf-8-sig')); fig,ax=plt.subplots(figsize=(7,8))
 for ft in b['features']:
  g=shape(ft['geometry']); gs=list(g.geoms) if hasattr(g,'geoms') else [g]
  for q in gs:x,y=q.exterior.xy;ax.plot(x,y,color='#8a8a8a',lw=.35)
 x=np.array([f(r['anchor_longitude']) for r in rows]);y=np.array([f(r['anchor_latitude']) for r in rows]);s=10+5*np.sqrt(np.array([f(r['gwis_geometry_area_ha']) for r in rows]));ax.scatter(x,y,s=s,color='#bd3d3a',edgecolor='white',linewidth=.25,alpha=.75);ax.set(xlabel='Longitude',ylabel='Latitude',title='Thermal anchors of GWIS events in the primary cohort');ax.set_aspect('equal');fig.tight_layout();fig.savefig(OUT/'Figure_1_thermal_anchors.png',dpi=600);plt.close(fig)
 fig,axs=plt.subplots(2,2,figsize=(9,7)); pairs=[('vpd_mean_first_24h_kpa','Mean VPD over first 24 h','log_geometry_area_ha','Log reconstructed geometry area'),('wind_mean_first_24h_ms','Mean wind over first 24 h','log_geometry_area_ha','Log reconstructed geometry area'),('vpd_mean_first_24h_kpa','Mean VPD over first 24 h','log1p_detection_interval_hours','Log one plus detection interval'),('wind_mean_first_24h_ms','Mean wind over first 24 h','log1p_detection_interval_hours','Log one plus detection interval')]
 for ax,(xx,xl,yy,yl) in zip(axs.flat,pairs):
  a=np.array([f(r[xx]) for r in rows]);c=np.array([f(r[yy]) for r in rows]);ax.scatter(a,c,s=20,color='#2962a3',alpha=.75);q=np.polyfit(a,c,1);z=np.linspace(a.min(),a.max(),100);ax.plot(z,q[0]*z+q[1],color='#222',lw=1);ax.set(xlabel=xl,ylabel=yl)
 fig.suptitle('Unadjusted descriptive associations',y=1.01);fig.tight_layout();fig.savefig(OUT/'Figure_2_unadjusted_associations.png',dpi=600,bbox_inches='tight');plt.close(fig)
 focus=[r for r in primary if r['term'] in {'vpd_mean_first_24h_kpa','wind_mean_first_24h_ms'}]; label={'log_geometry_area_ha':'Log geometry area','log1p_detection_interval_hours':'Log one plus detection interval','vpd_mean_first_24h_kpa':'Mean VPD','wind_mean_first_24h_ms':'Mean wind'}; labs=[f"{label[r['outcome']]}\n{label[r['term']]}" for r in focus];e=np.array([f(r['coefficient']) for r in focus]);lo=np.array([f(r['confidence_interval_95_lower']) for r in focus]);hi=np.array([f(r['confidence_interval_95_upper']) for r in focus]);pos=np.arange(len(focus));fig,ax=plt.subplots(figsize=(9,4.5));ax.errorbar(e,pos,xerr=np.vstack((e-lo,hi-e)),fmt='o',color='#8d253d',capsize=3);ax.axvline(0,color='#333',lw=.8);ax.set(yticks=pos,yticklabels=labs,xlabel='Adjusted coefficient per predictor IQR with 95 percent confidence interval',title='Primary models with HC3 standard errors');ax.invert_yaxis();fig.tight_layout();fig.savefig(OUT/'Figure_3_primary_coefficients.png',dpi=600);plt.close(fig)
 meta={p.name:list(Image.open(p).info.get('dpi',(0,0))) for p in OUT.glob('*.png')}
 if any(min(v)<599 for v in meta.values()):raise RuntimeError(meta)
 (OUT/'README.md').write_text('# English publication figures\n\nAll three figures are generated from verified analysis outputs at 600 dpi. Figure 2 is descriptive and unadjusted.\n',encoding='utf-8');print(json.dumps(meta,indent=2))
if __name__=='__main__':main()
