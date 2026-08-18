content = open('agent/watcher.py').read()

old = '''BROWSER_HISTORY_PATHS = {
    "Chrome": os.path.expandvars(
        r"%LOCALAPPDATA%\\Google\\Chrome\\User Data\\Default\\History"
    ),
    "Edge": os.path.expandvars(
        r"%LOCALAPPDATA%\\Microsoft\\Edge\\User Data\\Default\\History"
    )
}'''

new = '''BROWSER_HISTORY_PATHS = {
    "Chrome": os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "Google", "Chrome", "User Data", "Default", "History"
    ),
    "Edge": os.path.join(
        os.path.expanduser("~"), "AppData", "Local",
        "Microsoft", "Edge", "User Data", "Default", "History"
    )
}'''

content = content.replace(old, new)
open('agent/watcher.py', 'w').write(content)
print('Done!')

