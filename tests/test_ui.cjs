// Optional UI test: NODE_PATH=<path-to-jsdom/node_modules> node tests/test_ui.cjs
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const {JSDOM} = require('jsdom');
const html = fs.readFileSync(path.join(__dirname, '..', 'web.html'), 'utf8');
let globalSettings = {codec:'h264',resolution:'1280x720',video_bitrate:3333,audio_bitrate:144,preset:'fast',fps:30};
const requests = [];
const dom = new JSDOM(html, {url:'http://127.0.0.1:8080/', runScripts:'dangerously', beforeParse(w) {
  w.setInterval = () => 0;
  w.confirm = () => true;
  w.fetch = async (url, init) => {
    if (init?.method === 'POST') {
      const body = JSON.parse(init.body); requests.push({url,body});
      if (url === '/settings') {
        globalSettings = {...body, video_bitrate:body.video_bitrate==='auto'?null:Number(body.video_bitrate), audio_bitrate:Number(body.audio_bitrate), fps:Number(body.fps)};
        return {ok:true,json:async()=>globalSettings};
      }
      return {ok:true,json:async()=>({id:'1'.repeat(32)})};
    }
    const result = url==='/settings' ? globalSettings : url==='/jobs' ? [] : url==='/disk' ? {low:false,volumes:[]} : {operations:{backup_configured:false,notification_configured:false}};
    return {ok:true,json:async()=>result};
  };
}});
const tick = () => new Promise(r=>setTimeout(r,20));
const change = (el, value) => {el.value=value; el.dispatchEvent(new dom.window.Event('change',{bubbles:true}));};
(async()=>{
  await tick();
  const d=dom.window.document, el=id=>d.getElementById(id);
  const jobOptions=[...el('job-resolution').options].map(o=>o.value);
  assert.deepStrictEqual(jobOptions,['','1280x800','1920x1080','2560x1440','2560x1080','3440x1440']);
  assert.equal(el('global-resolution').value,'1280x720');
  assert.equal(el('global-resolution').selectedOptions[0].disabled,true);
  assert.equal(el('global-video').value,'custom');
  assert.equal(el('global-video-custom').value,'3333');
  assert.equal(el('global-audio-custom').value,'144');
  assert.equal(el('global-fps').value,'30');
  assert(el('defaults-note').textContent.includes('30 FPS'));
  assert(el('settings-form').checkValidity(), 'legacy selected value must remain savable');
  // Saving unchanged legacy global settings does not discard old values.
  el('settings-form').requestSubmit(); await tick();
  assert.equal(requests.at(-1).body.resolution,'1280x720');
  assert.equal(requests.at(-1).body.video_bitrate,'3333');
  // New global values, including a custom audio bitrate, are saved and shown.
  change(el('global-resolution'),'2560x1440'); change(el('global-video'),'8000');
  change(el('global-audio'),'custom'); el('global-audio-custom').value='177'; change(el('global-fps'),'60');
  el('settings-form').requestSubmit(); await tick();
  assert.deepStrictEqual(JSON.parse(JSON.stringify(requests.at(-1).body)), {codec:'h264',resolution:'2560x1440',video_bitrate:'8000',audio_bitrate:'177',fps:'60',preset:'fast'});
  assert(el('defaults-note').textContent.includes('60 FPS'));
  assert.equal(el('global-resolution').querySelector('[data-legacy]'),null);
  // Job-specific preset/custom values are sent without modifying the globals.
  el('url').value='https://example.org/playback/presentation/2.3/'+'a'.repeat(40)+'-1234567890123';
  change(el('job-resolution'),'2560x1080'); change(el('job-video'),'custom'); el('job-video-custom').value='3333';
  change(el('job-audio'),'192'); change(el('job-fps'),'25'); change(el('job-preset'),'slow');
  assert(el('form').checkValidity()); el('form').requestSubmit(); await tick();
  const req=requests.find(x=>x.url==='/jobs');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(req.body.settings)), {resolution:'2560x1080',fps:'25',preset:'slow',video_bitrate:'3333',audio_bitrate:'192'});
  assert.equal(globalSettings.fps,60);
  console.log('UI tests passed'); dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
