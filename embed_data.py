"""
Embed articles.json into dashboard.html as inline data.

Safe to run repeatedly. Always:
  1. Reads the latest articles.json
  2. Removes any existing embedded-data block
  3. Ensures the inline-data loader is present
  4. Inserts the fresh data block
"""
import json
import re

with open('articles.json') as f:
    data = json.load(f)

with open('dashboard.html') as f:
    html = f.read()

# Remove any existing embedded-data block
html = re.sub(
    r'<script id="embedded-data" type="application/json">.*?</script>\s*',
    '',
    html,
    flags=re.DOTALL,
)

# Ensure dashboard uses the inline-data loader
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

# Build the fresh data block
inline_block = (
    '<script id="embedded-data" type="application/json">\n'
    + json.dumps(data, indent=2)
    + '\n</script>\n'
)

# Insert before the main <script>
marker = '<script>\nlet DATA = null;'
if marker in html:
    html = html.replace(marker, inline_block + marker)
else:
    html = html.replace('</body>', inline_block + '</body>')

with open('dashboard.html', 'w') as f:
    f.write(html)

print(f"Embedded {len(data['articles'])} articles into dashboard.html")
print(f"File size: {len(html):,} bytes")
