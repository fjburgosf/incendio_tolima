"""Generate numbered tables and 600 dpi figures from executed D1 results."""
from __future__ import annotations
import csv, hashlib, json, os
from pathlib import Path
os.environ.setdefault('MPLBACKEND','Agg')
os.environ.setdefault('MPLCONFIGDIR',str(Path(__file__).resolve().parents[1]/'logs'/'matplotlib_config'))
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image
from shapely.geometry import shape

ROOT=Path(__file__).resolve().parents[1]
DATA=ROOT/'data'/'processed'/'gwis_event_analysis_v1.csv'
PRIMARY=ROOT/'results'/'primary'/'primary_models_v1.csv'
ROBUST=ROOT/'results'/'robustness'/'primary_robustness_v1.csv'
BOUNDARY=ROOT/'data'/'raw'/'boundaries'/'cortolima_limite_municipal_25k.geojson'
TABLES=ROOT/'tables'; FIGURES=ROOT/'figures'; AUDIT=ROOT/'results'/'primary'/'audit_tables_figures_v1.json'
def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest().upper()
def read(p):
    with p.open(encoding='utf-8-sig',newline='') as h:return list(csv.DictReader(h))
def write(p,rows):
    with p.open('x',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def f(x): return float(x)
def main():
    outputs=[TABLES/'Table_01_cohort_descriptives.csv',TABLES/'Table_02_primary_models.csv',TABLES/'Table_03_robustness.csv',FIGURES/'Figure_01_primary_event_anchors.png',FIGURES/'Figure_02_raw_associations.png',FIGURES/'Figure_03_primary_coefficients.png',AUDIT]
    if any(p.exists() for p in outputs[3:]): raise FileExistsError('Refusing to overwrite a numbered figure or audit')
    if not all(p.exists() for p in outputs[:3]) and any(p.exists() for p in outputs[:3]): raise FileExistsError('Incomplete existing table set')
    TABLES.mkdir(exist_ok=True); FIGURES.mkdir(exist_ok=True); (ROOT/'results'/'primary').mkdir(parents=True,exist_ok=True)
    rows=[r for r in read(DATA) if r['primary_cohort'].lower()=='true']; primary=read(PRIMARY); robust=read(ROBUST)
    variables=['gwis_geometry_area_ha','gwis_detection_interval_hours','vpd_mean_first_24h_kpa','wind_mean_first_24h_ms','anchor_elevation_m']
    t1=[]
    for var in variables:
        a=np.asarray([f(r[var]) for r in rows]);t1.append({'variable':var,'n':len(a),'minimum':a.min(),'q25':np.quantile(a,.25),'median':np.median(a),'q75':np.quantile(a,.75),'maximum':a.max()})
    if not outputs[0].exists(): write(outputs[0],t1)
    t2=[]
    for r in primary:
        t2.append({k:r[k] for k in ['outcome','term','coefficient','robust_standard_error_hc3','confidence_interval_95_lower','confidence_interval_95_upper','p_value_two_sided','bh_q_value_primary_family','n','r_squared','adjusted_r_squared']})
    if not outputs[1].exists(): write(outputs[1],t2)
    t3=[]
    for r in robust:
        if r['term'] in {'vpd_mean_first_24h_kpa','wind_mean_first_24h_ms'}:
            t3.append({k:r[k] for k in ['sensitivity','outcome','term','n','coefficient','confidence_interval_95_lower','confidence_interval_95_upper','p_value_two_sided']})
    if not outputs[2].exists(): write(outputs[2],t3)
    boundary=json.loads(BOUNDARY.read_text(encoding='utf-8-sig'))
    fig,ax=plt.subplots(figsize=(7,8))
    for ft in boundary['features']:
        g=shape(ft['geometry']); geoms=list(g.geoms) if hasattr(g,'geoms') else [g]
        for q in geoms:
            x,y=q.exterior.xy;ax.plot(x,y,color='#8a8a8a',lw=.35)
    x=np.asarray([f(r['anchor_longitude']) for r in rows]);y=np.asarray([f(r['anchor_latitude']) for r in rows]);s=10+5*np.sqrt(np.asarray([f(r['gwis_geometry_area_ha']) for r in rows]))
    ax.scatter(x,y,s=s,facecolor='#bd3d3a',edgecolor='white',linewidth=.25,alpha=.75)
    ax.set(xlabel='Longitud',ylabel='Latitud',title='Anclas térmicas de los eventos GWIS del análisis primario');ax.set_aspect('equal');fig.tight_layout();fig.savefig(outputs[3],dpi=600);plt.close(fig)
    fig,axs=plt.subplots(2,2,figsize=(9,7))
    pairs=[('vpd_mean_first_24h_kpa','VPD medio 24 h', 'log_geometry_area_ha','Log área geométrica'),('wind_mean_first_24h_ms','Viento medio 24 h','log_geometry_area_ha','Log área geométrica'),('vpd_mean_first_24h_kpa','VPD medio 24 h','log1p_detection_interval_hours','Log uno más intervalo'),('wind_mean_first_24h_ms','Viento medio 24 h','log1p_detection_interval_hours','Log uno más intervalo')]
    for ax,(xx,xl,yy,yl) in zip(axs.flat,pairs):
        a=np.asarray([f(r[xx]) for r in rows]);b=np.asarray([f(r[yy]) for r in rows]);ax.scatter(a,b,s=20,color='#2962a3',alpha=.75);c=np.polyfit(a,b,1);z=np.linspace(a.min(),a.max(),100);ax.plot(z,c[0]*z+c[1],color='#222222',lw=1);ax.set(xlabel=xl,ylabel=yl)
    fig.suptitle('Asociaciones crudas, no ajustadas',y=1.01);fig.tight_layout();fig.savefig(outputs[4],dpi=600,bbox_inches='tight');plt.close(fig)
    focus=[r for r in primary if r['term'] in {'vpd_mean_first_24h_kpa','wind_mean_first_24h_ms'}]; labels=[f"{r['outcome']}\n{r['term']}" for r in focus];est=np.asarray([f(r['coefficient']) for r in focus]);lo=np.asarray([f(r['confidence_interval_95_lower']) for r in focus]);hi=np.asarray([f(r['confidence_interval_95_upper']) for r in focus]);pos=np.arange(len(focus))
    fig,ax=plt.subplots(figsize=(9,4.5));ax.errorbar(est,pos,xerr=np.vstack((est-lo,hi-est)),fmt='o',color='#8d253d',ecolor='#8d253d',capsize=3);ax.axvline(0,color='#333333',lw=.8);ax.set(yticks=pos,yticklabels=labels,xlabel='Coeficiente ajustado por IQR con intervalo de confianza 95 por ciento',title='Modelos primarios con errores HC3');ax.invert_yaxis();fig.tight_layout();fig.savefig(outputs[5],dpi=600);plt.close(fig)
    dpi={p.name:list(Image.open(p).info.get('dpi',(0,0))) for p in outputs[3:6]}
    if any(min(v)<599 for v in dpi.values()):raise RuntimeError(f'DPI verification failed {dpi}')
    audit={'inputs':{str(p.relative_to(ROOT)):sha(p) for p in [DATA,PRIMARY,ROBUST,BOUNDARY]},'cohort_n':len(rows),'tables':[str(p.relative_to(ROOT)) for p in outputs[:3]],'figures':{p.name:{'file':str(p.relative_to(ROOT)),'sha256':sha(p),'dpi':dpi[p.name]} for p in outputs[3:6]},'interpretation_limit':'Figures display operational GWIS outcomes and reanalysis covariates. Raw scatter panels are not adjusted estimates. They do not establish causation or validate burned area.'}
    AUDIT.write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');print(json.dumps({'cohort_n':len(rows),'dpi':dpi},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
