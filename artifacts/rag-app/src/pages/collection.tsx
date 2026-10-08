import { useState, type ChangeEvent, type FormEvent, type ReactNode } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Link, useLocation, useParams } from 'wouter';
import { ArrowLeft, Check, CircleAlert, FilePlus2, FileSearch, LoaderCircle, Pencil, RefreshCw, Search, Send, Trash2, UploadCloud, X } from 'lucide-react';
import {
  getGetCollectionQueryKey, getGetDashboardQueryKey, getGetDocumentQueryKey, getListActivityQueryKey, getListCollectionsQueryKey, getListDocumentsQueryKey,
  useDeleteCollection, useDeleteDocument, useGetCollection, useGetDocument, useGetSystemConfig, useListDocuments, useQueryCollection, useRequestUploadUrl, useRetryDocumentIngestion, useSearchCollection, useUpdateCollection, useUploadDocument,
} from '@workspace/api-client-react';
import type { QueryResponse, SearchResponse } from '@workspace/api-client-react';
import { Button, EmptyState, ErrorState, FileGlyph, formatBytes, Modal, relativeTime, SkeletonBlock, StatusBadge, useNotice } from '@/components/workspace';

type Tab='documents'|'answer'|'retrieval';
export default function CollectionPage() {
  const {collectionId=''}=useParams<{collectionId:string}>();
  const [,setLocation]=useLocation();
  const client=useQueryClient();
  const collectionQuery=useGetCollection(collectionId,{query:{queryKey:getGetCollectionQueryKey(collectionId),enabled:!!collectionId}});
  const documentsQuery=useListDocuments(collectionId,{limit:100},{query:{queryKey:getListDocumentsQueryKey(collectionId,{limit:100}),enabled:!!collectionId,refetchInterval:query=>query.state.data?.items.some(d=>d.status==='queued'||d.status==='processing')?4500:false}});
  const systemConfig=useGetSystemConfig();
  const [documentDetailId,setDocumentDetailId]=useState('');
  const detailQuery=useGetDocument(documentDetailId,{query:{queryKey:getGetDocumentQueryKey(documentDetailId),enabled:!!documentDetailId}});
  const requestUpload=useRequestUploadUrl();
  const uploadDocument=useUploadDocument();
  const deleteDocument=useDeleteDocument();
  const retryDocument=useRetryDocumentIngestion();
  const updateCollection=useUpdateCollection();
  const deleteCollection=useDeleteCollection();
  const search=useSearchCollection();
  const answerQuery=useQueryCollection();
  const {showNotice,toast}=useNotice();
  const [tab,setTab]=useState<Tab>('documents');
  const [modal,setModal]=useState<'upload'|'edit'|'delete-collection'|'delete-document'|''>('');
  const [selectedDelete,setSelectedDelete]=useState<{id:string;name:string}|null>(null);
  const [name,setName]=useState('');
  const [description,setDescription]=useState('');
  const [file,setFile]=useState<File|null>(null);
  const [uploadError,setUploadError]=useState('');
  const [uploading,setUploading]=useState(false);
  const [question,setQuestion]=useState('');
  const [searchText,setSearchText]=useState('');
  const [answer,setAnswer]=useState<QueryResponse|null>(null);
  const [searchResult,setSearchResult]=useState<SearchResponse|null>(null);
  const [documentFilter,setDocumentFilter]=useState('');
  const collection=collectionQuery.data;
  const docs=documentsQuery.data?.items ?? [];
  const visibleDocs=docs.filter(d=>d.filename.toLowerCase().includes(documentFilter.toLowerCase()));
  const refresh=()=>{collectionQuery.refetch();documentsQuery.refetch();};
  const invalidateDocuments=async()=>Promise.all([
    client.invalidateQueries({queryKey:getListDocumentsQueryKey(collectionId,{limit:100})}),
    client.invalidateQueries({queryKey:getGetCollectionQueryKey(collectionId)}),
    client.invalidateQueries({queryKey:getListCollectionsQueryKey({limit:100})}),
    client.invalidateQueries({queryKey:getGetDashboardQueryKey()}),
    client.invalidateQueries({queryKey:getListActivityQueryKey({limit:8})}),
  ]);
  const openEdit=()=>{setName(collection?.name??'');setDescription(collection?.description??'');setModal('edit')};
  const saveCollection=(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();updateCollection.mutate({collectionId,data:{name:name.trim(),description:description.trim()}},{onSuccess:async()=>{await invalidateDocuments();setModal('');showNotice('Collection details saved')},onError:()=>showNotice('Could not update this collection')})};
  const confirmDeleteCollection=()=>deleteCollection.mutate({collectionId},{onSuccess:async()=>{await client.invalidateQueries({queryKey:getListCollectionsQueryKey({limit:100})});await client.invalidateQueries({queryKey:getGetDashboardQueryKey()});setLocation('/');showNotice('Collection deleted')},onError:()=>showNotice('Could not delete this collection')});
  const upload=async()=>{
    if(!file)return;
    setUploading(true);setUploadError('');
    try{
      const configResult=await systemConfig.refetch();
      if(!configResult.data)throw new Error('Provider status could not be checked. No file was uploaded; please try again.');
      if(!configResult.data.embeddings.configured)throw new Error('Document indexing is unavailable. Configure EMBEDDING_PROVIDER=openai-compatible, EMBEDDING_BASE_URL, EMBEDDING_MODEL, and EMBEDDING_DIMENSIONS=1536 (plus EMBEDDING_API_KEY if required), then retry.');
      const contentType=file.type||'application/octet-stream';
      const signed=await requestUpload.mutateAsync({data:{name:file.name,size:file.size,contentType}});
      const put=await fetch(signed.uploadURL,{method:'PUT',headers:{'Content-Type':contentType},body:file});
      if(!put.ok)throw new Error(`Direct upload failed (${put.status})`);
      await uploadDocument.mutateAsync({collectionId,data:{name:file.name,size:file.size,contentType,objectPath:signed.objectPath}});
      await invalidateDocuments();setFile(null);setModal('');showNotice('Document uploaded and queued for indexing');
    }catch(error){setUploadError(error instanceof Error?error.message:'The upload could not be completed.');}
    finally{setUploading(false)}
  };
  const runSearch=(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();if(!searchText.trim())return;search.mutate({collectionId,data:{query:searchText.trim(),top_k:5}},{onSuccess:setSearchResult,onError:()=>showNotice('Retrieval could not be completed. Check service readiness.')})};
  const runAnswer=(e:FormEvent<HTMLFormElement>)=>{e.preventDefault();if(!question.trim())return;answerQuery.mutate({collectionId,data:{question:question.trim(),top_k:5}},{onSuccess:r=>{setAnswer(r);showNotice(r.outcome==='no_sources'?'No source passages matched this question':'Answer ready with source citations')},onError:()=>showNotice('Answer service is unavailable. Your question has not been saved.')})};
  const doDeleteDocument=()=>{if(!selectedDelete)return;deleteDocument.mutate({documentId:selectedDelete.id},{onSuccess:async()=>{await invalidateDocuments();setModal('');setSelectedDelete(null);showNotice('Document removed from this collection')},onError:()=>showNotice('Could not remove document')})};
  const doRetry=(id:string)=>retryDocument.mutate({documentId:id},{onSuccess:async()=>{await invalidateDocuments();showNotice('Ingestion retry queued')},onError:()=>showNotice('Retry could not be queued')});
  const fileChanged=(e:ChangeEvent<HTMLInputElement>)=>setFile(e.target.files?.[0]??null);
  if(collectionQuery.isLoading&&!collection) return <><div className="page-head"><div><div className="eyebrow">COLLECTION / LOADING</div><h1 className="page-title">Opening collection</h1></div></div><SkeletonBlock rows={7}/></>;
  if(collectionQuery.isError&&!collection) return <ErrorState message="This collection could not be opened. It may have been removed or you may not have access." retry={refresh}/>;
  if(!collection)return <ErrorState message="Collection not found." retry={()=>setLocation('/')}/>;
  const selectedDetail=detailQuery.data?.id===documentDetailId ? detailQuery.data : undefined;
  return <>
    <div className="collection-detail-head">
      <div><Link href="/" className="eyebrow" style={{textDecoration:'none',display:'inline-flex',alignItems:'center',gap:6}}><ArrowLeft size={12}/> ALL COLLECTIONS</Link><h1 className="page-title">{collection.name}</h1><p className="page-subtitle">{collection.description||'A focused space for maintained team knowledge.'}</p><div className="collection-detail-meta"><span>{collection.document_count} documents</span><span>·</span><span>Updated {relativeTime(collection.updated_at)}</span></div></div>
      <div style={{display:'flex',gap:8,flexWrap:'wrap'}}><Button variant="outline" onClick={openEdit}><Pencil size={13}/> Edit details</Button><Button variant="quiet" onClick={()=>setModal('delete-collection')}><Trash2 size={14}/> Delete</Button></div>
    </div>
    <div className="tab-row" role="tablist" aria-label="Collection workspace">
      <TabButton active={tab==='documents'} onClick={()=>setTab('documents')} icon={<FilePlus2 size={14}/>} label="Documents"/>
      <TabButton active={tab==='answer'} onClick={()=>setTab('answer')} icon={<Send size={14}/>} label="Ask with sources"/>
      <TabButton active={tab==='retrieval'} onClick={()=>setTab('retrieval')} icon={<FileSearch size={14}/>} label="Inspect retrieval"/>
    </div>
    {tab==='documents' && <>
      <div className="collection-toolbar"><div className="search-wrap"><Search size={14}/><input className="search-input" value={documentFilter} onChange={e=>setDocumentFilter(e.target.value)} placeholder="Filter documents by filename" aria-label="Filter documents"/></div><span className="section-kicker">{docs.length} SOURCES</span><Button onClick={()=>{setFile(null);setUploadError('');setModal('upload')}}><UploadCloud size={14}/> Upload document</Button></div>
      {documentsQuery.isError&&!documentsQuery.data&&<ErrorState message="Documents could not be loaded." retry={()=>documentsQuery.refetch()}/>}
      {documentsQuery.isLoading&&!documentsQuery.data?<SkeletonBlock rows={5}/>:docs.length===0?<section className="section-panel"><EmptyState title="Bring the source material" description="Upload a PDF, Word document, or text file. The file goes directly to private storage, then indexing makes it searchable." action={<Button onClick={()=>setModal('upload')}><UploadCloud size={14}/> Upload first document</Button>}/></section>: visibleDocs.length===0?<section className="section-panel"><EmptyState title="No filenames found" description="Try a different search term." action={<Button variant="outline" onClick={()=>setDocumentFilter('')}>Clear filter</Button>}/></section>:
        <section className="section-panel"><div className="table-scroll"><table className="document-table"><thead><tr><th>Document</th><th>Status</th><th>Size</th><th>Chunks / pages</th><th>Updated</th><th></th></tr></thead><tbody>{visibleDocs.map(d=><tr key={d.id} data-testid={`row-document-${d.id}`}><td><button style={{padding:0,border:0,background:'none',cursor:'pointer'}} className="document-name" onClick={()=>setDocumentDetailId(d.id)}><FileGlyph mediaType={d.media_type}/><span>{d.filename}<span style={{display:'block',fontSize:9,color:'#9ba39c',fontWeight:400,marginTop:3}}>{d.media_type||'Unknown file type'}</span></span></button></td><td><StatusBadge status={d.status}/>{d.failure_code&&<div style={{fontSize:9,color:'#a65645',marginTop:5}}><CircleAlert size={11} style={{verticalAlign:'middle',marginRight:4}}/>{d.failure_code}</div>}</td><td className="mono">{formatBytes(d.byte_size)}</td><td className="mono">{d.chunk_count} chunks{d.page_count!==null&&d.page_count!==undefined?` · ${d.page_count} pages`:''}</td><td className="mono">{relativeTime(d.updated_at)}</td><td><div className="doc-actions">{d.status==='failed'&&<button className="icon-button" aria-label={`Retry ingestion for ${d.filename}`} title="Retry ingestion" disabled={retryDocument.isPending} onClick={()=>doRetry(d.id)}><RefreshCw size={13}/></button>}<button className="icon-button" aria-label={`Remove ${d.filename}`} title="Remove document" onClick={()=>{setSelectedDelete({id:d.id,name:d.filename});setModal('delete-document')}}><Trash2 size={13}/></button></div></td></tr>)}</tbody></table></div></section>}
      {docs.some(d=>d.status==='processing'||d.status==='queued')&&<div className="notice" style={{marginTop:14,display:'flex',alignItems:'center',gap:8}}><LoaderCircle size={13}/> Indexing runs in the background. Status refreshes automatically.</div>}
    </>}
    {tab==='answer'&&<div className="detail-grid">
      <section className="section-panel"><div className="section-heading"><div><h2 className="section-title">Ask this collection</h2><div className="section-kicker" style={{marginTop:4}}>GROUNDED ANSWERS WITH TRACEABLE CITATIONS</div></div></div>
        <form className="qa-form" onSubmit={runAnswer}><label className="label" htmlFor="question-input">What do you need to know?</label><textarea id="question-input" className="textarea" maxLength={4000} value={question} onChange={e=>setQuestion(e.target.value)} placeholder="Ask a specific question about the documents in this collection."/><div className="form-row"><span className="hint">Answers are limited to this collection's ready sources.</span><Button type="submit" disabled={!question.trim()||answerQuery.isPending}>{answerQuery.isPending?<><LoaderCircle size={14}/>Finding sources…</>:<><Send size={13}/> Ask question</>}</Button></div></form>
        {answer&&<div className="answer-box"><div className="answer-label"><Check size={13}/> {answer.outcome==='answered'?'Grounded answer':'No sources found'} <span style={{marginLeft:'auto'}}>{answer.retrieved_count} passages{answer.latency_ms!==undefined?` · ${answer.latency_ms} ms`:''}</span></div><div className="answer-copy">{answer.answer||'No answer could be grounded in the ready documents.'}</div>{answer.citations.map((c,i)=><article className="citation" key={c.citation_id}><div className="citation-head"><span>[{c.citation_id}] {c.document_name}</span><span>{c.page_number?`Page ${c.page_number} · `:''}score {c.score.toFixed(2)}</span></div><p>{c.excerpt}</p></article>)}</div>}
      </section>
      <section className="section-panel"><div className="section-heading"><div><h2 className="section-title">Answer integrity</h2><div className="section-kicker" style={{marginTop:4}}>WHAT THIS WORKSPACE WILL AND WON'T DO</div></div></div><div style={{padding:20}}><IntegrityRow title="Collection-scoped" text="Retrieval uses only ready documents in this collection."/><IntegrityRow title="Sources shown" text="Each citation points to a retrieved passage and source document."/><IntegrityRow title="No match, no claim" text="When relevant passages are missing, the answer can say so."/><div className="notice" style={{marginTop:17}}>Review citations against the original maintained source before relying on consequential decisions.</div></div></section>
    </div>}
    {tab==='retrieval'&&<section className="section-panel"><div className="section-heading"><div><h2 className="section-title">Inspect retrieval</h2><div className="section-kicker" style={{marginTop:4}}>RANKED SOURCE PASSAGES · NO ANSWER GENERATION</div></div></div><form className="qa-form" onSubmit={runSearch}><label className="label" htmlFor="retrieval-input">Search the collection</label><textarea className="textarea" id="retrieval-input" maxLength={4000} value={searchText} onChange={e=>setSearchText(e.target.value)} placeholder="Enter a question or phrase to inspect matching passages." style={{minHeight:76}}/><div className="form-row"><span className="hint">Top 5 passages · scores shown for inspection</span><Button type="submit" disabled={!searchText.trim()||search.isPending}>{search.isPending?<><LoaderCircle size={14}/>Searching…</>:<><Search size={13}/> Run retrieval</>}</Button></div></form>
      {searchResult&&<div className="search-results"><div className="section-kicker" style={{marginBottom:8}}>{searchResult.results.length} MATCHING PASSAGES</div>{searchResult.results.length ? searchResult.results.map((r,i)=><article className="result-item" key={r.chunk_id}><div className="result-meta"><span>{String(i+1).padStart(2,'0')} / {r.document_name}{r.page_number?` · PAGE ${r.page_number}`:''}</span><span>SCORE {r.score.toFixed(3)}</span></div><div className="result-content">{r.content}</div><div className="hint" style={{marginTop:8}}>Chunk {r.chunk_id}{r.start_offset!==null&&r.start_offset!==undefined?` · characters ${r.start_offset}–${r.end_offset??'—'}`:''}</div></article>) : <EmptyState title="No matching passages" description="Try a more specific term or verify that documents have completed indexing."/>}</div>}
      {!searchResult&&<EmptyState title="See why something matched" description="Run a retrieval inspection to see ranked excerpts, source documents, page references, and match scores."/>}
    </section>}
    {modal==='upload'&&<Modal title="Upload a source document" onClose={()=>!uploading&&setModal('')} footer={<><Button variant="outline" disabled={uploading} onClick={()=>setModal('')}>Cancel</Button><Button disabled={!file||uploading} onClick={upload}>{uploading?<><LoaderCircle size={14}/>Uploading…</>:<><UploadCloud size={14}/>Upload and queue</>}</Button></>}>
      {systemConfig.data&&!systemConfig.data.embeddings.configured&&<div role="status" className="notice" style={{marginBottom:15,background:'#f5e7e1',color:'#8f493b'}}>Indexing is unavailable until an embeddings provider is configured. No file will be transferred.</div>}
      <div className="notice" style={{marginBottom:15}}>File bytes are sent directly to private storage using a short-lived signed URL. Only metadata is sent to the workspace API.</div><label className="file-drop" htmlFor="document-file"><UploadCloud size={21}/><strong>{file?.name??'Choose a document'}</strong><span>{file?`${formatBytes(file.size)} · ${file.type||'File type detected from extension'}`:'PDF, DOCX, TXT or another supported source'}</span><input id="document-file" type="file" onChange={fileChanged} style={{position:'absolute',width:1,height:1,opacity:0}}/></label>{file&&<div className="upload-file-row"><span>{file.name}</span><button className="icon-button" aria-label="Remove selected file" onClick={()=>setFile(null)}><X size={13}/></button></div>}{uploadError&&<div role="alert" className="notice" style={{marginTop:13,background:'#f5e7e1',color:'#a34e3e'}}>{uploadError}</div>}</Modal>}
    {modal==='edit'&&<Modal title="Edit collection details" onClose={()=>setModal('')} footer={<><Button variant="outline" onClick={()=>setModal('')}>Cancel</Button><Button type="submit" form="edit-collection" disabled={!name.trim()||updateCollection.isPending}>{updateCollection.isPending?'Saving…':'Save changes'}</Button></>}><form id="edit-collection" onSubmit={saveCollection}><div className="field"><label className="label" htmlFor="edit-name">Name</label><input id="edit-name" required maxLength={120} value={name} onChange={e=>setName(e.target.value)}/></div><div className="field"><label className="label" htmlFor="edit-description">Description</label><textarea id="edit-description" maxLength={1000} value={description} onChange={e=>setDescription(e.target.value)}/></div></form></Modal>}
    {modal==='delete-collection'&&<Modal title="Delete this collection?" onClose={()=>setModal('')} footer={<><Button variant="outline" onClick={()=>setModal('')}>Keep collection</Button><Button variant="danger" onClick={confirmDeleteCollection} disabled={deleteCollection.isPending}>{deleteCollection.isPending?'Deleting…':'Delete collection'}</Button></>}><p className="page-subtitle">“{collection.name}” and its documents will be scheduled for deletion. This cannot be undone.</p></Modal>}
    {modal==='delete-document'&&selectedDelete&&<Modal title="Remove document?" onClose={()=>setModal('')} footer={<><Button variant="outline" onClick={()=>setModal('')}>Keep document</Button><Button variant="danger" onClick={doDeleteDocument} disabled={deleteDocument.isPending}>{deleteDocument.isPending?'Removing…':'Remove document'}</Button></>}><p className="page-subtitle">“{selectedDelete.name}” will no longer be available in this collection. This cannot be undone.</p></Modal>}
    {documentDetailId&&<Modal title="Document record" onClose={()=>setDocumentDetailId('')} footer={<Button variant="outline" onClick={()=>setDocumentDetailId('')}>Close</Button>}>{detailQuery.isLoading&&!selectedDetail?<SkeletonBlock rows={5}/>:detailQuery.isError||!selectedDetail?<ErrorState message="The document record could not be loaded." retry={()=>detailQuery.refetch()}/>:<><div className="upload-file-row"><span>Filename</span><strong>{selectedDetail.filename}</strong></div><div className="upload-file-row"><span>Ingestion status</span><StatusBadge status={selectedDetail.status}/></div><div className="upload-file-row"><span>Media type</span><span>{selectedDetail.media_type}</span></div><div className="upload-file-row"><span>File size</span><span>{formatBytes(selectedDetail.byte_size)}</span></div><div className="upload-file-row"><span>Indexed chunks</span><span>{selectedDetail.chunk_count}</span></div><div className="upload-file-row"><span>Pages</span><span>{selectedDetail.page_count??'Not provided'}</span></div><div className="upload-file-row"><span>Added</span><span>{new Date(selectedDetail.created_at).toLocaleString()}</span></div>{selectedDetail.failure_code&&<div role="alert" className="notice" style={{marginTop:14,background:'#f5e7e1',color:'#a34e3e'}}>Failure code: {selectedDetail.failure_code}</div>}</>}</Modal>}
    {toast}
  </>;
}

function TabButton({active,onClick,icon,label}:{active:boolean;onClick:()=>void;icon:ReactNode;label:string}) {return <button role="tab" aria-selected={active} className={`tab-button ${active?'active':''}`} onClick={onClick}>{icon}{label}</button>}
function IntegrityRow({title,text}:{title:string;text:string}){return <div style={{padding:'10px 0',borderBottom:'1px solid #eeeae1'}}><div style={{fontSize:11,color:'#475f5d',fontWeight:600}}>{title}</div><div style={{fontSize:10,color:'#89938b',marginTop:4,lineHeight:1.5}}>{text}</div></div>}
