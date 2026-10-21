"""Pixel runs in an opaque-origin sandbox, never in the claim-token document."""

PIXEL_ID = "3714470908697594"

CONSENT_HTML = """
<section id="analytics-consent">
  <p>Дозволити рекламну аналітику Meta? Meta отримає дані відвідування та,
  після підтвердження, суму й валюту покупки. Це не впливає на доступ до клубу.</p>
  <button id="analytics-yes" type="button">Дозволити</button>
  <button id="analytics-no" type="button">Відхилити</button>
</section>
"""

PARENT_SCRIPT = """
(() => {
  const token = new URL(location.href).searchParams.get('token');
  let frame, attempts = 0, finished = false, busy = false;
  async function purchase() {
    if (finished || busy || attempts++ >= 40) return;
    busy = true;
    try {
      const response = await fetch('/checkout/meta-purchase', {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token})
      });
      if (response.ok) {
        const event = await response.json();
        if (event) {
          finished = true;
          frame.contentWindow.postMessage({type: 'purchase', event}, '*');
        }
      }
    } catch (_) { /* Analytics must never interrupt activation. */ }
    busy = false;
    if (!finished) setTimeout(purchase, 3000);
  }
  window.addEventListener('message', event => {
    if (frame && event.source === frame.contentWindow && event.data === 'pixel-ready') {
      purchase();
    }
  });
  document.getElementById('analytics-no').onclick = () => {
    document.getElementById('analytics-consent').remove();
  };
  document.getElementById('analytics-yes').onclick = () => {
    document.getElementById('analytics-consent').remove();
    frame = document.createElement('iframe');
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.referrerPolicy = 'no-referrer';
    frame.hidden = true;
    frame.src = '/tracking/meta';
    document.body.appendChild(frame);
  };
})();
"""


def pixel_frame(nonce: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Neurokolo analytics</title></head>
<body><script nonce="{nonce}">
!function(f,b,e,v,n,t,s){{if(f.fbq)return;n=f.fbq=function(){{n.callMethod?
n.callMethod.apply(n,arguments):n.queue.push(arguments)}};if(!f._fbq)f._fbq=n;
n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;
t.src=v;t.onload=function(){{parent.postMessage('pixel-ready','*')}};
s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}}
(window,document,'script','https://connect.facebook.net/en_US/fbevents.js');
fbq('set', 'autoConfig', false, '{PIXEL_ID}');
fbq('init', '{PIXEL_ID}');
fbq('track', 'PageView');
let sent = false;
window.addEventListener('message', function(message) {{
  if (message.source !== parent || sent || message.data?.type !== 'purchase') return;
  const event = message.data.event;
  if (!event || typeof event.value !== 'number' || typeof event.currency !== 'string'
      || !/^purchase_[a-f0-9-]{{36}}$/.test(event.event_id)) return;
  sent = true;
  fbq('track', 'Purchase', {{value: event.value, currency: event.currency}},
      {{eventID: event.event_id}});
}});
</script></body></html>"""
