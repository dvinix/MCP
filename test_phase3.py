"""Quick automated test for Phase 3 — commitment extraction."""
import subprocess
import json
import sys
import time

# Simulate: person selection (1) → 5 test messages → exit
test_input = """1
I'll handle the plumber call this week
someone needs to call the electrician about the flickering lights
ok I got Thursday pickup covered for the kids
I'll deal with the insurance thing by Friday
I'll schedule the dentist appointment
exit
"""

proc = subprocess.Popen(
    [sys.executable, "host.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    cwd="D:\\MCP",
    text=True,
)

stdout, _ = proc.communicate(input=test_input, timeout=60)

print("=== HOST OUTPUT ===")
print(stdout[-3000:] if len(stdout) > 3000 else stdout)

# Check what got saved
try:
    with open("D:\\MCP\\commitments.json") as f:
        commitments = json.load(f)
    print(f"\n=== COMMITMENTS SAVED ({len(commitments)}) ===")
    for c in commitments:
        print(f"  Person: {c.get('person','?'):30s} Task: {c.get('task','')}")
        print(f"  Deadline: {c.get('deadline','(none)'):20s} Source: {c.get('source_text','')[:80]}")
        print()
except FileNotFoundError:
    print("\n=== NO commitments.json found ===")
