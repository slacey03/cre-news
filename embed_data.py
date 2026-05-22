"""
Embed articles.json into dashboard.html as inline data.

This script is safe to run repeatedly. It always:
  1. Reads the latest articles.json
  2. Removes any existing embedded-data block from dashboard.html
  3. Ensures the dashboard has the inline-data loader (replacing fetch loader if present)
  4. Inserts the fresh data block
"""
import json
import re

with open('articles.json') as f:
    data = json.load(f)

with open('dashboard.html') as f:
    html = f.read()

# --- 1. Remove any existing embedded-data block ---
html = re.sub(
    r'<script id="embedded-data" type="application/json">.*?</script>\s*',
    '',
    html,
    flags=re.DOTALL,
)

# --- 2. Ensure dashboard uses the inline-data loader (idempotent) ---
fetch_loader = """async function loadData() {
  try {
    const res = await fetch('articles.json');
    DATA = await res.json();
    renderStats();
    render();
  } catch (err) {
    document.getElementById('articles').innerHTML =
      '<div class="empty">Could not load articles.json. Run <code>python scraper.py</code> first.</div>';
  }
}"""

inline_loader = """function loadData() {
  try {
    const el = document.getElementById('embedded-data');
    if (el) {
      DATA = JSON.parse(el.textContent);
      renderStats();
      render();
      return;
    }
    fetch('articles.json').then(r => r.json()).then(d => {
      DATA = d;
      renderStats();
      render();
    }).catch(() => {
      document.getElementById('articles').innerHTML =
        '<div class="empty">No data available.</div>';
    });
  } catch (err) {
    document.getElementById('articles').innerHTML =
      '<div class="empty">Error loading data: ' + err.message + '</div>';
  }
}"""

if fetch_loader in html:
    html = html.replace(fetch_loader, inline_loader)

# --- 3. Build the fresh data block ---
inline_block = (
    '<script id="embedded-data" type="application/json">\n'
    + json.dumps(data, indent=2)
    + '\n</script>\n'
)

# --- 4. Insert the data block right before the main <script> that defines DATA ---
marker = '<script>\nlet DATA = null;'
if marker in html:
    html = html.replace(marker, inline_block + marker)
else:
    # Fallback: insert before closing </body>
    html = html.replace('</body>', inline_block + '</body>')

with open('dashboard.html', 'w') as f:
    f.write(html)

print(f"Embedded {len(data['articles'])} articles into dashboard.html")
print(f"File size: {len(html):,} bytes")
