import sys, json, collections, time
sys.path.insert(0, '.')
import config
from utils.pdf_reader import extract_pdf
from agents.image_agent import _is_relevant_image, _coverage_reduce_images, _triage_image
pdf = sys.argv[1]; label = sys.argv[2]
doc = extract_pdf(pdf, render_graph_pages=True)
raw = [(p, im) for p in doc.pages for im in p.images if _is_relevant_image(im)]
cov = _coverage_reduce_images(raw)
passed = [it for it in (_triage_image(p, im) for p, im in cov) if it['passed']]
print(json.dumps({'label': label, 'pages': len(doc.pages), 'raw_relevant': len(raw), 'after_coverage': len(cov), 'triage_passed': len(passed), 'batches@8': -(-len(passed) // 8)}, ensure_ascii=False))
