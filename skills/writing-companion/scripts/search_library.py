"""Find catalog titles or exact text with reproducible local line locations."""
import argparse,json,sys
from pathlib import Path

def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('query',nargs='?',default='')
 p.add_argument('--workspace',type=Path)
 p.add_argument('--title',help='Filter by requested title, source title, author, or source ID')
 p.add_argument('--source',help='Exact source ID')
 p.add_argument('--limit',type=int,default=12)
 p.add_argument('--context',type=int,default=3)
 args=p.parse_args()
 if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
 root=args.workspace
 if root is None:
  if (Path.cwd()/'workspace.json').is_file():root=Path.cwd()
  else:
   binding=Path(__file__).resolve().parents[1]/'local-workspace.json'
   if not binding.is_file():p.error('Specify --workspace, run from a workspace, or configure local-workspace.json')
   root=Path(json.loads(binding.read_text('utf-8-sig'))['default_workspace'])
 root=root.expanduser().resolve()
 manifest=json.loads((root/'workspace.json').read_text('utf-8-sig'))
 catalog=json.loads((root/manifest['catalog']).read_text('utf-8-sig'))
 sources=catalog['sources']
 if args.source:sources=[s for s in sources if s['id']==args.source]
 if args.title:
  needle=args.title.casefold()
  sources=[s for s in sources if needle in json.dumps([s['title'],s['id'],s.get('authors',[]),s.get('requested_titles',[])],ensure_ascii=False).casefold()]
 if not args.query:
  for s in sources[:max(1,args.limit)]:print(json.dumps(s,ensure_ascii=False))
  print(json.dumps({'matched_sources':len(sources),'mode':'catalog','note':'标题匹配不表示全文或指定版本已完整获取。'},ensure_ascii=False))
  return
 found=0;read=0;query=args.query.casefold();context=max(0,min(args.context,20))
 for s in sources:
  path=root/s['text_path'];lines=path.read_text('utf-8').splitlines();read+=1
  for i,line in enumerate(lines):
   if query not in line.casefold():continue
   lo=max(0,i-context);hi=min(len(lines),i+context+1)
   print(json.dumps({'source_id':s['id'],'title':s['title'],'path':str(path.resolve()),'match_line':i+1,'line_start':lo+1,'line_end':hi,'context':'\n'.join(f'{j+1}: {lines[j]}' for j in range(lo,hi))},ensure_ascii=False))
   found+=1
   if found>=max(1,args.limit):break
  if found>=max(1,args.limit):break
 print(json.dumps({'matches_shown':found,'sources_scanned':read,'candidate_sources':len(sources),'limit_reached':found>=max(1,args.limit),'method':'case-insensitive literal substring; simplified/traditional forms are distinct'},ensure_ascii=False))
if __name__=='__main__':main()
