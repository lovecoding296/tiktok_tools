(() => {
  const log = (...a) => console.log('%c[YT-LINK]', 'color:#09f;font-weight:bold', ...a);
  const BASE = 'https://www.youtube.com/';
  // @handle (cho phép ký tự unicode) hoặc /channel/UCxxxx
  const RE = /^\/(@[^\/?#"'\s\\<>]+|channel\/UC[\w-]{22})/;

  log('URL trang:', location.href);
  log('Tổng số thẻ <a href>:', document.querySelectorAll('a[href]').length);

  const toLink = href => {
    if (!href) return null;
    let path = href;
    try { path = new URL(href, location.origin).pathname; } catch (e) {}
    path = decodeURIComponent(path);
    const m = path.match(RE);
    return m ? BASE + m[1] : null;
  };

  const fromAnchors = (selector, name) => {
    const els = document.querySelectorAll(selector);
    const set = new Set();
    const samples = [];
    els.forEach(a => {
      const l = toLink(a.getAttribute('href'));
      if (l) set.add(l);
      if (samples.length < 5) samples.push(a.getAttribute('href'));
    });
    log(`[${name}] khớp selector: ${els.length} thẻ -> ${set.size} link kênh`);
    if (els.length && !set.size) log(`[${name}] vài href mẫu (không khớp regex):`, samples);
    return set;
  };

  // Bước 1: thẻ kênh (giao diện cũ + mới)
  const cardSel = [
    'ytd-channel-renderer a[href]',
    'ytd-grid-channel-renderer a[href]',
    'ytd-guide-entry-renderer a[href]',
    'yt-lockup-view-model a[href]',
    'ytd-video-owner-renderer a[href]',
  ].join(',');
  let result = fromAnchors(cardSel, 'Bước 1: thẻ kênh');

  // Bước 2: mọi thẻ <a>
  if (!result.size) {
    result = fromAnchors('a[href]', 'Bước 2: mọi thẻ a');
  }

  // Bước 3: quét text HTML (kể cả link nằm trong JSON/attribute)
  if (!result.size) {
    const html = document.documentElement.innerHTML;
    const set = new Set();
    for (const m of html.matchAll(/\/(@[^\/?#"'\s\\<>&]+|channel\/UC[\w-]{22})/g)) {
      set.add(BASE + decodeURIComponent(m[1]));
    }
    log(`[Bước 3: quét text HTML] -> ${set.size} link kênh`);
    result = set;
  }

  const links = [...result];
  log(`KẾT QUẢ: ${links.length} link`);
  if (!links.length) {
    log('Vẫn 0 link. Kiểm tra: (1) ô chọn ngữ cảnh trên Console phải là "top"; (2) trang đã tải xong chưa; (3) có đang ở trang có danh sách kênh không.');
    return;
  }

  console.table(links.map((l, i) => ({ '#': i + 1, link: l })));

  const text = links.join('\n');
  try {
    copy(text);
    log('Đã copy bằng copy()');
  } catch (e) {
    navigator.clipboard.writeText(text)
      .then(() => log('Đã copy bằng clipboard API'))
      .catch(() => log('Không copy được, hãy copy thủ công từ bảng phía trên hoặc từ biến window.__links'));
  }
  window.__links = links; // gõ __links.join('\n') nếu cần lấy lại
})();