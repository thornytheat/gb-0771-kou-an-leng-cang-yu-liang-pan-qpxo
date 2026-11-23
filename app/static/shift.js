import {selectLot} from './tracks.js';
selectLot();
const dataset = document.querySelector('#dataset');
const status = document.querySelector('#status');
dataset.addEventListener('change', () => location.assign(`/view/${document.body.dataset.view}?sample=${dataset.value}`));
async function file(button, path, payload) {
  button.disabled = true;
  status.textContent = '复算中…';
  try {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message);
    status.textContent = result.reused ? '复算一致' : '归档完成';
    if (button.id === 'file-receipt') location.assign(`/view/history?${new URLSearchParams({receipt:result.id,sample:dataset.value})}`);
  } catch (error) {
    status.textContent = error.message || '请重试归档';
  } finally { button.disabled = false; }
}
document.querySelector('#docket').addEventListener('submit', event => {
  event.preventDefault(); file(event.submitter, '/api/receipts', {sample:dataset.value});
});
document.querySelector('#refresh')?.addEventListener('click', event => file(event.currentTarget, `/api/receipts/${event.currentTarget.dataset.id}/refresh`, null));

const correction = document.querySelector('#correction');
correction?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = correction.querySelector('button');
  const status = document.querySelector('#correction-status');
  const payload = Object.fromEntries(new FormData(correction).entries());
  button.disabled = true;
  status.textContent = '跨班次重算中…';
  try {
    const response = await fetch('/api/receipts/corrections', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.message);
    if (result.idempotent) {
      status.textContent = result.status === 'noop' ? '门区已是更正值，无新版本' : '同一更正已生效，未新增版本';
    } else {
      status.textContent = `已生效，关联班次 ${result.receipts.length} 个`;
      const newest = result.receipts[result.receipts.length - 1];
      setTimeout(() => location.assign(`/view/history?${new URLSearchParams({receipt: newest.id, sample: dataset.value})}`), 600);
    }
  } catch (error) {
    status.textContent = `重算失败，各关联班次保留旧版：${error.message || '请重试'}`;
  } finally { button.disabled = false; }
});
