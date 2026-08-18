content = open('agent/watcher.py').read()

old = '''PORTAL_PATTERNS = {
    "GeM": [
        {"domain": "gem.gov.in", "params": ["q", "search", "keyword"]},
    ],
    "KPPP": [
        {"domain": "kppp.karnataka.gov.in", "params": ["search", "keyword", "q"]},
    ]
}'''

new = '''PORTAL_PATTERNS = {
    "GeM": [
        {"domain": "bidplus.gem.gov.in", "params": ["search_keyword", "q", "search", "keyword"]},
        {"domain": "gem.gov.in", "params": ["search_keyword", "q", "search", "keyword"]},
    ],
    "KPPP": [
        {"domain": "kppp.karnataka.gov.in", "params": ["search", "keyword", "q"]},
    ]
}'''

content = content.replace(old, new)
open('agent/watcher.py', 'w').write(content)
print('Done!') 
