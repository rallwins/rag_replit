import { useQueryClient } from '@tanstack/react-query';
import { Check, CircleAlert, Database, Fingerprint, Gauge, Layers3, RefreshCw, ShieldCheck, Sparkles, Workflow } from 'lucide-react';
import { getGetSystemConfigQueryKey, getHealthCheckQueryKey, getReadinessCheckQueryKey, useGetSystemConfig, useHealthCheck, useReadinessCheck } from '@workspace/api-client-react';
import { Button, ErrorState, SkeletonBlock } from '@/components/workspace';

export default function SettingsPage() {
  const client=useQueryClient();
  const health=useHealthCheck();
  const readiness=useReadinessCheck();
  const config=useGetSystemConfig();
  const refresh=()=>{
    client.invalidateQueries({queryKey:getHealthCheckQueryKey()});
    client.invalidateQueries({queryKey:getReadinessCheckQueryKey()});
    client.invalidateQueries({queryKey:getGetSystemConfigQueryKey()});
  };
  const hasError=health.isError&&!health.data&&readiness.isError&&!readiness.data&&config.isError&&!config.data;
  if(hasError)return <ErrorState message="System diagnostics are unavailable right now." retry={refresh}/>;
  const loading=(health.isLoading&&!health.data)||(readiness.isLoading&&!readiness.data)||(config.isLoading&&!config.data);
  if(loading)return <><div className="page-head"><div><div className="eyebrow">WORKSPACE / OPERATIONS</div><h1 className="page-title">System readiness</h1><p className="page-subtitle">Provider and dependency health, without exposing credentials.</p></div></div><SkeletonBlock rows={7}/></>;
  const cfg=config.data;
  const read=readiness.data;
  const available=read?.status==='ready';
  const serviceItems=[
    {name:'Answer generation',icon:Sparkles,provider:cfg?.llm.provider,model:cfg?.llm.model,configured:cfg?.llm.configured,caption:'Language model'},
    {name:'Embeddings',icon:Layers3,provider:cfg?.embeddings.provider,model:cfg?.embeddings.model,configured:cfg?.embeddings.configured,caption:`${cfg?.embeddings.dimensions??'—'} dimensions`},
    {name:'Document storage',icon:Database,provider:cfg?.storage.backend,model:'Private object storage',configured:cfg?.storage.configured,caption:'Storage backend'},
    {name:'Session security',icon:Fingerprint,provider:cfg?.auth.mode,model:'Session verification',configured:cfg?.auth.configured,caption:'Authentication mode'},
  ];
  const checks=read?.checks?Object.entries(read.checks):[];
  return <>
    <div className="page-head"><div><div className="eyebrow">WORKSPACE / OPERATIONS</div><h1 className="page-title">System readiness</h1><p className="page-subtitle">Provider and dependency health, without exposing credentials.</p></div><Button variant="outline" onClick={refresh}><RefreshCw size={13}/> Refresh status</Button></div>
    {(config.isError||readiness.isError||health.isError)&&<div className="notice" role="status" style={{marginBottom:16}}>Some diagnostics could not be reached. Available status details are shown below.</div>}
    <section className="section-panel" style={{padding:21,display:'flex',justifyContent:'space-between',alignItems:'center',gap:22}}>
      <div style={{display:'flex',alignItems:'center',gap:14}}><div style={{width:43,height:43,borderRadius:9,display:'grid',placeItems:'center',background:available?'#e8f0e7':'#f4eedc',color:available?'#5c7d62':'#917840'}}>{available?<ShieldCheck size={21}/>:<CircleAlert size={21}/>}</div><div><div className="eyebrow">DEPENDENCY READINESS</div><div style={{font:'500 23px var(--app-font-serif)',color:'#2b4950',marginTop:3}}>{read?.status==='ready'?'All systems ready':read?.status==='not_ready'?'Action required':'Readiness unknown'}</div><div style={{fontSize:11,color:'#87928b',marginTop:4}}>A service-ready check covers the providers used for retrieval and answers.</div></div></div>
      <span className={`service-badge ${available?'':'off'}`}><span className="health-dot" style={{display:'inline-block',marginRight:6,background:available?'#658a61':'#c19c4f'}}/>{read?.status??'unknown'}</span>
    </section>
    <div className="settings-grid">{serviceItems.map(item=><ServiceCard key={item.name} {...item}/>)}</div>
    <div className="health-layout">
      <section className="section-panel"><div className="section-heading"><div><h2 className="section-title">Dependency checks</h2><div className="section-kicker" style={{marginTop:4}}>READINESS PROBE</div></div><Workflow size={15} color="#73857e"/></div><div className="health-summary">{checks.length?checks.map(([name,status])=><div className="health-check" key={name}><span>{name.replace(/[_-]/g,' ')}</span><strong style={{color:status==='ok'?'#5e8064':status==='misconfigured'?'#a65645':'#987a3d'}}>{status}</strong></div>):<p className="page-subtitle">No individual dependency checks were returned.</p>}</div></section>
      <section className="section-panel"><div className="section-heading"><div><h2 className="section-title">Service health</h2><div className="section-kicker" style={{marginTop:4}}>PROCESS LIVENESS</div></div><Gauge size={15} color="#73857e"/></div><div className="health-summary"><div className="health-check"><span>API process</span><strong style={{color:health.data?.status==='ok'?'#5e8064':'#987a3d'}}>{health.data?.status??(health.isError?'unavailable':'unknown')}</strong></div><div className="health-check"><span>Dependencies included</span><strong>no · liveness only</strong></div><p className="page-subtitle" style={{marginTop:13}}>Liveness confirms the API process is responding. Readiness above is the more complete signal for document and answer operations.</p></div></section>
    </div>
    <div className="notice" style={{marginTop:18,display:'flex',gap:9,alignItems:'flex-start'}}><Check size={14} style={{flex:'none',marginTop:1}}/><span>Diagnostics show only provider names, model identifiers, configured flags, and health state. No API keys, tokens, endpoints containing secrets, or credentials are displayed here.</span></div>
  </>;
}

function ServiceCard({name,icon:Icon,provider,model,configured,caption}:{name:string;icon:typeof Database;provider?:string;model?:string;configured?:boolean;caption:string}) {
  return <section className="section-panel service-card"><div className="service-top"><div style={{display:'flex',alignItems:'center',gap:10}}><div className="collection-icon"><Icon size={16}/></div><div className="service-title">{name}</div></div><span className={`service-badge ${configured?'':'off'}`}>{configured?'configured':'not configured'}</span></div><div className="service-detail"><span>Provider / mode</span><strong>{provider||'Not reported'}</strong></div><div className="service-detail" style={{paddingTop:10,marginTop:9}}><span>{caption}</span><strong>{model||'Not reported'}</strong></div></section>;
}
