import sys, json, collections, time
sys.path.insert(0, '.')
import fitz, config
from utils.pdf_reader import extract_pdf
from utils.document_objects import build_document_objects
from utils.object_routing import deduplicate_evidence_objects
from utils.selective_ocr import build_triage_plan, native_confidence, reconstruct_candidate_regions
from agents.extractor_agent import _build_page_text
pdf = sys.argv[1]; label = sys.argv[2]
t0 = time.time()
doc = extract_pdf(pdf, render_graph_pages=True)
pages = doc.pages
raw = build_document_objects(pages)
objs, dups = deduplicate_evidence_objects(raw)
plan = build_triage_plan(objs, backend='vlm', confidence_threshold=float(config.OCR_NATIVE_CONFIDENCE_THRESHOLD))
out = {'label': label, 'pages': len(pages), 'raw_objects': len(raw), 'physical_objects': len(objs), 'dups': dups}
out['by_type'] = collections.Counter(o.object_type for o in objs)
out['action_by_type'] = collections.Counter(f'{r.object_type}:{r.action}' for r in plan)
req = [r for r in plan if r.action == 'ocr_required']
out['ocr_required'] = len(req)
out['required_reason_top'] = collections.Counter(reason for r in req for reason in r.reasons).most_common(25)
out['required_table_conf_buckets'] = collections.Counter(round(r.native_confidence, 1) for r in req if r.object_type == 'table')
out['required_caption_only'] = sum(r.caption_only for r in req)
out['required_missing_native'] = sum(r.missing_native for r in req)
# region variants per required object (no rendering)
pmap = {p.page_number: p for p in pages}
omap = {o.object_id: o for o in objs}
by_page = collections.defaultdict(list)
for o in objs: by_page[o.page_number].append(o)
pdfdoc = fitz.open(pdf)
variants = collections.Counter(); total_regions = 0
for r in req:
    o = omap.get(r.object_id); p = pmap.get(r.page_number)
    if o is None or p is None: continue
    regs = reconstruct_candidate_regions(pdfdoc[r.page_number - 1], p, o, by_page[r.page_number])
    total_regions += len(regs)
    for g in regs: variants[g.variant] += 1
out['render_regions_total'] = total_regions
out['render_variants'] = variants
# embedded images relevant + page renders
out['page_images_total'] = sum(len(p.images or []) for p in pages)
out['page_render_images'] = sum(1 for p in pages for im in (p.images or []) if str(im.get('source_kind') or '') == 'page_render' or 'full render' in str(im.get('caption', '')).lower())
# text batch sizes
sizes = [len(_build_page_text([p])) for p in pages]
out['pages_over_18000'] = sum(s > 18000 for s in sizes); out['pages_over_12000'] = sum(s > 12000 for s in sizes)
out['max_page_chars'] = max(sizes); out['pages_with_tables'] = sum(1 for p in pages if p.tables)
out['seconds'] = round(time.time() - t0, 1)
print(json.dumps(out, ensure_ascii=False, indent=1, default=lambda x: dict(x) if hasattr(x, 'items') else str(x)))
