import { Link } from 'wouter';
import { ArrowLeft, FileQuestion } from 'lucide-react';

export default function NotFound() {
  return <section className="section-panel" style={{maxWidth:620,margin:'8vh auto',padding:'40px 34px',textAlign:'center'}}>
    <div className="empty-icon" style={{width:54,height:54}}><FileQuestion size={23}/></div>
    <div className="eyebrow" style={{marginTop:20}}>FIELDNOTES / 404</div>
    <h1 className="page-title" style={{fontSize:34,marginTop:8}}>This page isn’t in the record.</h1>
    <p className="page-subtitle" style={{maxWidth:350,margin:'0 auto 22px'}}>The address may have changed, or this workspace never had that page.</p>
    <Link href="/" className="button button-primary"><ArrowLeft size={14}/> Back to overview</Link>
  </section>;
}
