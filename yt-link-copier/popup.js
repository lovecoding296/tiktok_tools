// Hàm này chạy TRONG trang web (được inject), trả về mảng link kênh.
function scrapeChannelLinks() {
  const BASE = 'https://www.youtube.com/';
  const RE = /^\/(@[^\/?#"'\s\\<>]+|channel\/UC[\w-]{22})/;

  const safeDecode = s => { try { return decodeURIComponent(s); } catch (e) { return s; } };

  const toLink = href => {
    if (!href) return null;
    let path = href;
    try { path = new URL(href, location.origin).pathname; } catch (e) {}
    const m = safeDecode(path).match(RE);
    return m ? BASE + m[1] : null;
  };

  const fromAnchors = selector => {
    const set = new Set();
    document.querySelectorAll(selector).forEach(a => {
      const l = toLink(a.getAttribute('href'));
      if (l) set.add(l);
    });
    return set;
  };

  // Bước 1: thẻ kênh (giao diện cũ + mới)
  const cardSel = [
    'ytd-channel-renderer a[href]',
    'ytd-grid-channel-renderer a[href]',
    'ytd-guide-entry-renderer a[href]',
    'yt-lockup-view-model a[href]',
    'ytd-video-owner-renderer a[href]'
  ].join(',');
  let result = fromAnchors(cardSel);

  // Bước 2: mọi thẻ <a>
  if (!result.size) result = fromAnchors('a[href]');

  // Bước 3: quét text HTML
  if (!result.size) {
    const html = document.documentElement.innerHTML;
    const set = new Set();
    for (const m of html.matchAll(/\/(@[^\/?#"'\s\\<>&]+|channel\/UC[\w-]{22})/g)) {
      set.add(BASE + safeDecode(m[1]));
    }
    result = set;
  }

  return [...result];
}

const btn = document.getElementById('copyBtn');
const status = document.getElementById('status');

function show(msg, cls) {
  status.textContent = msg;
  status.className = cls || '';
}

btn.addEventListener('click', async () => {
  btn.disabled = true;
  show('Đang quét...');
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !/^https?:\/\/([\w-]+\.)?youtube\.com\//.test(tab.url || '')) {
      show('Hãy mở trang YouTube', 'err');
      return;
    }

    const [{ result: links }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: scrapeChannelLinks
    });

    if (!links || !links.length) {
      show('Không tìm thấy link nào', 'err');
      return;
    }

    await navigator.clipboard.writeText(links.join('\n'));
    show(`Đã copy ${links.length} dòng`, 'ok');
  } catch (e) {
    show('Lỗi: ' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
});
