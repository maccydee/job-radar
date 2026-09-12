"""Interactive dashboard, rendered from the database.

Shares the CSS and the layout with the static renderer so the two views of the
same data cannot drift apart visually. The difference is that every row here
carries actions, and those actions write back.
"""

from __future__ import annotations

import html as _h
import json
from urllib.parse import quote
from collections import Counter
from datetime import datetime

from .. import employment, store
from .favicon import link_tag as _favicon_tag, mark as _favicon_mark
from .markdown import to_html as _md

_FAVICON = _favicon_tag()
_MARK = _favicon_mark()

from .html import _CSS, _cap_location, _SECTORS, _MODES, safe_url


def _js() -> str:
    """The dashboard script with the three costs filled in.

    Derived, never typed twice. `60_000` lived as a literal in three files
    until one of them was wrong, and a page quoting a token cost the tool no
    longer charges is worse than one quoting none: the reader budgets against
    it. `tests/test_dashboard_bulk.py` fails if a placeholder survives.
    """
    from .. import rank
    from ..runner import MAX_RUNNING
    from ..serve import BULK_LIMIT
    return (_JS.replace("%SCREEN_TOKENS%", str(rank.SCREEN_TOKENS))
               .replace("%BULK_LIMIT%", str(BULK_LIMIT))
               .replace("%MAX_RUNNING%", str(MAX_RUNNING)))


# How many rows are written with their action buttons already in them.
#
# Enough to cover the first screenful several times over, so a reader who
# never scrolls never sees a button appear. Everything past this gets them
# from `fillActs` on hover or focus, which is a fraction of a millisecond for
# one row and is why the page is not 60,000 nodes of controls nobody clicked.
EAGER_ROWS = 60

# The data attribute suffix for each kind of document, named explicitly rather
# than derived from the kind.
#
# `cover_letter` became `data-open-cover_letter`, which the browser exposes as
# `dataset.openCover_letter`, while the script asked for `openCoverLetter`.
# The two never met, so on every lazy row the cover letter showed a plain
# draft button instead of Download and Open, and clicking it would have paid
# to write a second one. The suffixes have no underscores now, so the mapping
# from attribute to dataset key is the identity.
DOC_ATTR = {"screen": "screen", "cv": "cv", "cover_letter": "letter"}

# Indexed by `eager`, so the marker is a lookup rather than an f-string that
# has to escape its own quotes. See the comment where it is used.
_LAZY_ATTR = {True: "", False: ' data-lazyacts="1"'}


_EXTRA_CSS = """
/* Whether anything is running at all, where you can see it without knowing
   which row to look at. A queue of nine screens is invisible if the only
   sign of it is a line on nine separate rows. */
#jobsinfo{margin-left:var(--s3);color:var(--accent);font-size:.8125rem;
  font-variant-numeric:tabular-nums}
#jobsinfo:empty{display:none}

/* "There is one already", on the title line where the other states are. */
.ready{display:inline-block;margin-left:var(--s2);padding:1px 7px;
  border:1px solid var(--accent);border-radius:999px;
  font-size:.6875rem;font-weight:600;letter-spacing:.02em;
  color:var(--accent);vertical-align:middle;white-space:nowrap}

/* Everything else that ages, beside the sources badge.

   Its own class rather than `.sync.warn`, which belongs to the source-list
   badge and which a test asserts on exactly: two elements wearing one class
   made that test unable to tell them apart, and a guard that cannot tell what
   it is looking at is not a guard. `margin-left:0` because `.sync` claims the
   whole gap with `margin-left:auto` and the second one would be pushed off
   the end of the row. */
.sync.agecheck{margin-left:var(--s3);color:#d98080;border-bottom-color:#d98080}
.sync.agecheck.broken{font-weight:600}

/* Contract and interim work, on the title line.

   Filled rather than outlined, unlike `.ready` above, and in `--flag` rather
   than `--accent`. The two pills sit side by side on the same rows and an
   outline in the same colour made them read as one control; this is a fact
   about the job, not a state of the tool. `--flag` exists in the dark
   palettes only, so the light value is set here alongside them. */
.emp{display:inline-block;margin-left:var(--s2);padding:1px 8px;
  border-radius:999px;background:#b06a12;color:#fff;
  font-size:.6875rem;font-weight:600;letter-spacing:.02em;
  vertical-align:middle;white-space:nowrap}
@media (prefers-color-scheme: dark){.emp{background:var(--flag);color:#1b1b1f}}
:root[data-theme="dark"] .emp{background:var(--flag);color:#1b1b1f}

/* The employment chips are a different question from the working-pattern
   chips directly above them, and two identical rows of pills read as one
   wrapped group. A little air, and nothing else. */
.chips.emps{margin-top:calc(var(--s2) * -1)}

/* The row the reload was for. Fades rather than staying marked, because a
   highlight that never clears becomes part of the furniture. */
.row.justdone{background:var(--surface-2);
  box-shadow:inset 3px 0 0 0 var(--accent);
  animation:justdone 12s ease-out forwards}
@keyframes justdone{0%,85%{background:var(--surface-2)}100%{background:transparent}}
@media (prefers-reduced-motion: reduce){.row.justdone{animation:none}}

.gateok{color:var(--muted);font-size:.75rem}

.jobprog{grid-column:1/-1;margin-top:var(--s2);font-size:.8125rem;
  color:var(--accent);font-variant-numeric:tabular-nums}

/* Shown while the browser builds the board.
   The server answers in under a second and the browser then spends most of
   another one parsing several thousand rows, which reads as a frozen tab
   rather than as work in progress. Painted before the list because it sits
   first in the body, and removed the moment the first filter pass finishes.

   `position:fixed` and its own background, so it covers a half-built list
   rather than sitting above one. */
.boot{position:fixed;inset:0;z-index:50;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:var(--s3);
  background:var(--bg);color:var(--muted);text-align:center;padding:var(--s4)}
.boot p{margin:0;font-size:.9375rem}
.boot .boot-sub{font-size:.8125rem;opacity:.7;max-width:34ch}
.boot-spin{width:26px;height:26px;border-radius:50%;
  border:2px solid var(--line);border-top-color:var(--accent);
  animation:bootspin .7s linear infinite}
@keyframes bootspin{to{transform:rotate(360deg)}}
/* A spinner that cannot spin is a still ring, which reads as broken. Pulse
   the opacity instead, which is motion nobody has asked us not to make. */
@media (prefers-reduced-motion: reduce){
  .boot-spin{animation:bootfade 1.4s ease-in-out infinite;border-top-color:var(--line)}
  @keyframes bootfade{0%,100%{opacity:.35}50%{opacity:1}}
}

/* Bulk selection. The checkbox is quiet until something is selected, because
   the row is the content and a column of ticked boxes down the left of a
   four thousand row board is noise on every visit. */
.pick{display:flex;align-items:center}
.pick input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer;
  opacity:.35;transition:opacity var(--dur) var(--ease)}
.row:hover .pick input,.pick input:checked,.pick input:focus-visible{opacity:1}
/* The bar only exists while something is picked, so it never takes space it
   is not using. */
.bulk{position:sticky;bottom:0;z-index:5;display:flex;flex-wrap:wrap;gap:var(--s3);
  align-items:center;justify-content:space-between;
  padding:var(--s3) var(--s4);margin-top:var(--s4);
  background:var(--surface);border:1px solid var(--accent);
  border-radius:var(--r-lg);box-shadow:var(--shadow)}
.bulk[hidden]{display:none}
.bulk .cost{color:var(--muted);font-size:.8125rem}
.bulk .warn{color:var(--accent);font-weight:600}

/* Actions. Kept quiet: the row is the content, these are what you do to it. */
.acts{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:var(--s2);margin-top:var(--s3)}
.acts button,.acts a.btn{border:1px solid var(--line);background:var(--surface);
  color:var(--muted);font:inherit;font-size:.8125rem;font-weight:500;
  letter-spacing:-.008em;padding:6px 12px;border-radius:var(--r-md);cursor:pointer;
  text-decoration:none;display:inline-flex;align-items:center;gap:6px;
  transition:color var(--dur) var(--ease),border-color var(--dur) var(--ease),
             background var(--dur) var(--ease)}
.acts button:hover,.acts a.btn:hover{color:var(--ink);border-color:var(--muted)}
.acts button.primary{color:var(--accent);border-color:var(--accent)}
.acts button.primary:hover{background:var(--accent);color:var(--surface)}
.acts button:disabled{opacity:.4;cursor:not-allowed}
.acts select{border:1px solid var(--line);background:var(--surface);color:var(--muted);
  font:inherit;font-size:.8125rem;padding:6px 10px;border-radius:var(--r-md);cursor:pointer}
.acts select:hover{color:var(--ink)}
.rownote{grid-column:1/-1;font-size:.8125rem;color:var(--muted);margin-top:var(--s2);
  font-style:italic}
.acts button:disabled:hover{color:var(--muted);border-color:var(--line)}
.acts button.busy{color:var(--accent);border-color:var(--accent)}
.acts button.busy::after{content:"";width:9px;height:9px;border-radius:50%;
  border:1.5px solid currentColor;border-top-color:transparent;
  animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){.acts button.busy::after{animation:none}}
.row.settled{opacity:.45}
.row.settled .role{text-decoration:line-through;text-decoration-thickness:1px}
.docs{grid-column:1/-1;display:flex;flex-wrap:wrap;gap:var(--s2);margin-top:var(--s2);
  font-size:.8125rem}
.docs a{color:var(--accent);text-decoration:none;border-bottom:1px solid transparent}
.docs a:hover{border-bottom-color:var(--accent)}
.docs .rating{color:var(--pay);font-weight:600;font-variant-numeric:tabular-nums}
.docs .gatefail{color:var(--flag)}
.err{grid-column:1/-1;font-size:.8125rem;color:var(--flag);margin-top:var(--s2)}
.toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);
  background:var(--ink);color:var(--surface);padding:10px 18px;border-radius:var(--r-pill);
  font-size:.875rem;box-shadow:var(--shadow-up);opacity:0;pointer-events:none;
  transition:opacity var(--dur) var(--ease);z-index:50;max-width:90vw;text-align:center}
.toast.show{opacity:1}
/* A facet whose count has fallen to nothing under the current tab. Left
   clickable, because one of them may be the filter you are already in. */
.chips button.none{opacity:.45}
"""

_JS = r"""
const $=s=>document.querySelector(s), toast=$('#toast');
// Opens on Open, not All. All includes skipped and rejected, and they sort by
// score like everything else, so a skipped role you already dismissed sat at
// the top of the board every time you refreshed.
let f='open', secs=new Set(), modes=new Set(), emps=new Set(), country='', city='';

// The board reloads itself when a screen or a CV finishes, to show the new
// document. Every reload threw the reader back to Open with no sector, no
// country and no city: you filtered down to eleven roles, paid to screen one,
// and landed back on four thousand. The reload is the right behaviour; losing
// where you were is not.
//
// localStorage, not the URL and not sessionStorage. Not the URL because this
// is where one reader is looking rather than a view worth sharing, and a link
// carrying somebody else's filters is its own surprise. Not sessionStorage,
// which was the first attempt: it dies with the tab, so it survived the
// board's own reloads and lost the view every time the browser was closed,
// which is most of when it matters. Wrapped, because a browser told to block
// site data throws on the accessor itself rather than returning nothing.
const VIEW_KEY='job-radar:view';
function saveView(){
  try{ localStorage.setItem(VIEW_KEY, JSON.stringify({
    f, secs:[...secs], modes:[...modes], emps:[...emps], country, city,
    sort:(document.querySelector('#fsort')||{}).value||''})); }catch(e){}}
function loadView(){
  let v=null;
  try{ v=JSON.parse(localStorage.getItem(VIEW_KEY)||'null'); }catch(e){}
  if(!v) return;
  // Each one guarded on its own. A filter for a city that no longer has a
  // role in it would otherwise restore an empty board and read as a scan
  // that found nothing.
  const has=(sel,val)=>!!document.querySelector(sel+' [value="'+CSS.escape(val)+'"]');
  if(v.f) { const b=document.querySelector('.seg button[data-f="'+CSS.escape(v.f)+'"]');
            if(b){ document.querySelectorAll('.seg button').forEach(o=>o.setAttribute('aria-selected','false'));
                   b.setAttribute('aria-selected','true'); f=v.f; } }
  if(v.country && has('#fcountry', v.country)){ country=v.country;
    const s=document.querySelector('#fcountry'); if(s) s.value=country; }
  if(v.city && has('#fcity', v.city)){ city=v.city;
    const s=document.querySelector('#fcity'); if(s) s.value=city; }
  for(const x of (v.secs||[])) secs.add(x);
  for(const x of (v.modes||[])) modes.add(x);
  for(const x of (v.emps||[])) emps.add(x);
  // Only chips that are still on the page. A sector whose last role went
  // settled has no chip this time round, and a set holding a key nothing can
  // clear is a filter the reader cannot see or switch off.
  const live=new Set(), liveModes=new Set(), liveEmps=new Set();
  for(const b of document.querySelectorAll('.chips button')){
    const key=b.dataset.sec||b.dataset.mode||b.dataset.emp;
    const set=b.dataset.sec?live:(b.dataset.mode?liveModes:liveEmps);
    set.add(key);
    const cur=b.dataset.sec?secs:(b.dataset.mode?modes:emps);
    if(cur.has(key)) b.setAttribute('aria-pressed','true'); }
  for(const x of [...secs]) if(!live.has(x)) secs.delete(x);
  for(const x of [...modes]) if(!liveModes.has(x)) modes.delete(x);
  for(const x of [...emps]) if(!liveEmps.has(x)) emps.delete(x);
  const sort=document.querySelector('#fsort');
  if(sort && v.sort){ const ok=[...sort.options].some(o=>o.value===v.sort);
                      if(ok) sort.value=v.sort; }}

function say(msg,ms=3200){toast.textContent=msg;toast.classList.add('show');
  clearTimeout(say._t);say._t=setTimeout(()=>toast.classList.remove('show'),ms);}

const SETTLED=new Set(['rejected','withdrawn','skipped','closed']);
// Applications with something still owed on them, either way.
const IN_FLIGHT=new Set(['applied','submitted','interviewing','offer']);
// Applications that ended. Not "skipped": you never applied to those.
const CLOSED_OUT=new Set(['rejected','withdrawn','closed']);
// The number on a facet has to be the number that facet will show you.
//
// These were counted once, in Python, over every row in the database, while
// the page opens on Open and Open hides everything settled. So a board with
// one skipped public-sector role rendered a chip reading "Public sector 1",
// and clicking it emptied the list and said "Nothing matches those filters".
// Same for the country and city menus. Counting here, against the rows the
// current tab admits, means the chip cannot promise a role the tab hides.
//
// Each dimension is counted with its own filter left off, because that is
// what clicking would do: sector chips are an OR within sectors, so with Tech
// selected the number on Finance is what adding Finance would bring in.
function paintCounts(sec,mode,emp,ctry,city){
  for(const b of document.querySelectorAll('.chips button')){
    const k=b.dataset.sec||b.dataset.mode||b.dataset.emp;
    const n=(b.dataset.sec?sec:(b.dataset.mode?mode:emp))[k]||0;
    const el=b.querySelector('.n'); if(el) el.textContent=n;
    b.classList.toggle('none',n===0);}
  for(const [q,m] of [['#fcountry',ctry],['#fcity',city]])
    for(const o of document.querySelectorAll(q+' option')){
      if(!o.value) continue;
      o.textContent=(o.dataset.label||o.value)+' ('+(m[o.value]||0)+')';}}

function apply(){let n=0;
  const cSec={},cMode={},cEmp={},cCountry={},cCity={};
  const bump=(m,k)=>{m[k]=(m[k]||0)+1};
  for(const r of document.querySelectorAll('.row')){
    const st=r.dataset.status;
    const viewOk = f==='all' || (f==='open' && !SETTLED.has(st)) ||
                   (f==='pay' && r.dataset.pay==='1') ||
                   (f==='new' && r.dataset.new==='1') ||
                   (f==='live' && IN_FLIGHT.has(st)) ||
                   (f==='closed' && CLOSED_OUT.has(st)) ||
                   (f==='fit' && (+r.dataset.fit)>=70);
    const okSec  = secs.size===0  || secs.has(r.dataset.sector);
    const okMode = modes.size===0 || modes.has(r.dataset.mode);
    // An empty set means every employment type, which is what a reader who
    // has not touched these chips is asking for. It must NOT default to
    // permanent: the whole point of the facet is that contract work is
    // findable, and a default that hid it would be the tool answering a
    // question nobody asked.
    const okEmp  = emps.size===0  || emps.has(r.dataset.emp||'unstated');
    const okCtry = !country || r.dataset.country===country;
    const okCity = !city    || r.dataset.city===city;
    if(viewOk&&okMode&&okEmp&&okCtry&&okCity) bump(cSec,r.dataset.sector);
    if(viewOk&&okSec &&okEmp&&okCtry&&okCity) bump(cMode,r.dataset.mode);
    if(viewOk&&okSec &&okMode&&okCtry&&okCity) bump(cEmp,r.dataset.emp||'unstated');
    if(viewOk&&okSec &&okMode&&okEmp&&okCity) bump(cCountry,r.dataset.country);
    if(viewOk&&okSec &&okMode&&okEmp&&okCtry) bump(cCity,r.dataset.city);
    const ok = viewOk && okSec && okMode && okEmp && okCtry && okCity;
    r.hidden=!ok; if(ok)n++;}
  paintCounts(cSec,cMode,cEmp,cCountry,cCity);
  saveView();
  $('#empty').hidden=n>0; $('#list').hidden=n===0;}

document.querySelectorAll('.seg button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.seg button').forEach(o=>o.setAttribute('aria-selected','false'));
  b.setAttribute('aria-selected','true'); f=b.dataset.f;
  // Choosing the Best fit tab implies you want fit order, but it no longer
  // owns it: the sort select below stays in charge everywhere, so you can
  // read New in fit order, which is the pairing you actually want.
  if(f==='fit'){const sel=$('#fsort'); if(sel) sel.value='fit';}
  resort(); apply();});
// Rank order, used everywhere except Best fit. Applications you are in come
// first because they are the ones with a deadline on them; things you settled
// go last because you have already decided. Score breaks the tie in between.
function tier(r){
  const st=r.dataset.status;
  if(IN_FLIGHT.has(st)) return 0;
  if(SETTLED.has(st)) return 2;
  return 1;}
const byRank=(a,b)=>tier(a)-tier(b)||(+b.dataset.score)-(+a.dataset.score);
// An unranked role stores fit -1. Sorting on that raw value buries every role
// you have not paid to rank yet below a role scored 0, which reads as "these
// are bad" rather than "these are unknown". They sort after the scored ones
// but ahead of a genuine zero, and keep their filter-score order among
// themselves so the list is still useful before you rank anything.
//
// This mapped -1 to -0.5, which is still below zero, so the fix did nothing:
// an unranked Head of Engineering with the second-highest filter score on the
// board sorted underneath a role that had been read and scored 0 with the
// reason "no people leadership at all". Fit scores are whole numbers, so any
// value strictly between 0 and 1 puts unknown exactly where the paragraph
// above says it belongs.
const fitOf=(r)=>{const v=+r.dataset.fit; return v<0 ? 0.5 : v;};
const byFit=(a,b)=>tier(a)-tier(b)
  ||fitOf(b)-fitOf(a)||(+b.dataset.score)-(+a.dataset.score);
// Salary, in the order the numbers can actually be trusted.
//
// Two things were wrong with sorting on one number. data-payfloor used to
// fall back to the top of the range when a posting stated no bottom, so
// "up to 175,000" claimed a floor of 175,000 and led the list ahead of a
// role guaranteeing 150,000-180,000 -- the vaguer advert winning on a figure
// nobody had promised. And the comparison ran across currencies, so 200,000
// of one thing outranked 175,000 of another purely on the digits, which is
// the exact guess `salary.clears_floor` refuses to make ("cross-currency
// comparison is refused rather than guessed at"). Those rows are already
// flagged "not compared" against the floor; ranking them by it anyway
// contradicted the flag on the same row.
//
// So: roles are grouped by how much their figure is worth relying on, and
// only compared inside a group. On a board that is all one currency, which
// is the normal case, this changes nothing.
//   3  a stated bottom of range, in the currency most of the board uses
//   2  a stated bottom of range, in some other currency
//   1  a stated top but no bottom, so no floor at all
//   0  no stated pay
// Group 2 was every other currency AT ONCE, and then the numbers inside it
// were compared as bare numbers. So on a board whose home currency is GBP,
// `INR 6,612,600` (about £62,000) sorted above `USD 260,000` (about
// £290,000), and `SGD 181,545` and `CAD 180,000` both outranked `USD
// 179,000`. That is exactly the conversion this comment says the sort stopped
// making, arrived at by grouping instead of by dividing.
//
// Every one of those rows already carries "not compared" against the floor,
// so the tool knew it could not rank them and ranked them anyway.
//
// Foreign rows now hold their group but fall back to the rank order the
// dashboard already computed, rather than to a number that means a different
// thing on every row. Ordering by score is not a claim about pay.
function payGroup(r){
  const floor=+r.dataset.payfloor||0;
  if(floor) return (!HOME_CUR || (r.dataset.paycur||'')===HOME_CUR) ? 3 : 2;
  return (+r.dataset.paytop||0) ? 1 : 0;}
function homePay(r){
  // Only a figure in the reader's own currency is a figure this can order by.
  const g=payGroup(r);
  return g===3 ? (+r.dataset.payfloor||0) : 0;}
function homeTop(r){
  return payGroup(r)===3 ? (+r.dataset.paytop||0) : 0;}
const bySalary=(a,b)=>tier(a)-tier(b)
  ||payGroup(b)-payGroup(a)
  ||homePay(b)-homePay(a)
  ||homeTop(b)-homeTop(a)
  ||(+b.dataset.score)-(+a.dataset.score);
const byNew=(a,b)=>tier(a)-tier(b)
  ||(b.dataset.seen||'').localeCompare(a.dataset.seen||'')
  ||(+b.dataset.score)-(+a.dataset.score);
const SORTS={rank:byRank, fit:byFit, salary:bySalary, new:byNew};
function resort(){
  const sel=$('#fsort'), list=$('#list');
  const cmp=SORTS[sel && sel.value] || byRank;
  [...list.querySelectorAll('.row')].sort(cmp).forEach(r=>list.appendChild(r));}

// Sort and filter once at load. The filter previously only ran on a click,
// which was invisible while the default view was All and everything showed
// anyway; the moment the default became Open, every settled role was still on
// screen until you touched a tab.
(function(){
  const sel=$('#fsort');
  if(sel) sel.onchange=()=>{resort(); apply();};
  loadView();
  resort(); apply();
  // After the first sort and filter pass, which is the point the board is
  // actually usable. The inline fallback above clears it on DOMContentLoaded,
  // which is earlier and would uncover a list still being sorted.
  const boot=document.getElementById('boot'); if(boot) boot.remove();
})();

document.querySelectorAll('.chips button').forEach(b=>b.onclick=()=>{
  const on=b.getAttribute('aria-pressed')==='true';
  b.setAttribute('aria-pressed', on?'false':'true');
  const set=b.dataset.sec?secs:(b.dataset.mode?modes:emps),
        key=b.dataset.sec||b.dataset.mode||b.dataset.emp;
  on?set.delete(key):set.add(key); apply();});
// Ranking spends tokens, so the click shows the cost and waits for a yes.
// Everything else that spends in this tool works the same way.
const rankBtn=$('#rank'), rankInfo=$('#rankinfo');
// Ranks what the Country filter shows, so a UK board is not charged for the
// American roles sitting beside it. The estimate asks with the same country.
function rankCountries(){ return (typeof country==='string' && country) ? [country] : []; }
async function rankState(){
  const c=rankCountries();
  const r=await fetch('/api/rank'+(c.length?'?country='+encodeURIComponent(c[0]):''));
  if(!r.ok) return null;
  return r.json();}
const stopBtn=$('#rankstop');
function mmss(t){const m=Math.floor(t/60),s=t%60;
  return m?`${m}m ${String(s).padStart(2,'0')}s`:`${s}s`;}

function paintRank(d, extra=0){
  if(d.state!=='running') return;
  const batch=Math.floor(d.done/d.batch_size)+1;
  const of=Math.ceil(d.total/d.batch_size);
  rankInfo.textContent = d.stopping
    ? `stopping after this batch (${d.done}/${d.total} done)`
    : `${d.done}/${d.total} scored · batch ${Math.min(batch,of)} of ${of} `+
      `in flight · ${mmss(d.elapsed+extra)}`;}

async function refreshRankInfo(){
  const d=await rankState(); if(!d) return d;
  _rank=d; _tick=0;
  if(d.state==='running'){
    rankBtn.classList.add('busy'); rankBtn.disabled=true;
    stopBtn.hidden=false;
    // The counter only moves once per batch, roughly two minutes apart. With
    // nothing else changing, a run in progress looked identical to one that
    // had hung, so say which batch is in flight and how long it has been.
    const batch=Math.floor(d.done/d.batch_size)+1;
    const of=Math.ceil(d.total/d.batch_size);
    rankInfo.textContent = d.stopping
      ? `stopping after this batch (${d.done}/${d.total} done)`
      : `${d.done}/${d.total} scored · batch ${Math.min(batch,of)} of ${of} `+
        `in flight · ${mmss(d.elapsed)}`;
  }else{
    rankBtn.classList.remove('busy'); rankBtn.disabled=false;
    stopBtn.hidden=true;
    // Say what is actually true. "0 unranked" was rendered as "everything is
    // ranked", while a quarter of the board carried no score at all because
    // those postings have no description to judge fit against.
    const bits=[];
    const where=rankCountries().length ? ` in ${rankCountries()[0]}` : '';
    if(d.pending) bits.push(`${d.pending} to rank${where}`);
    if(d.scored) bits.push(`${d.scored} ranked`);
    if(d.unrankable) bits.push(`${d.unrankable} listing-only, nothing to rank`);
    rankInfo.textContent = bits.join(' · ');
    // A run that stopped because the account ran out looks exactly like one
    // that finished, unless it says so.
    if(d.error) say(d.error, 12000);}
  return d;}
refreshRankInfo();

stopBtn.onclick=async ()=>{
  stopBtn.disabled=true;
  const {ok,data}=await post('/api/rank/stop',{});
  say(ok ? (data.message||'stopping') : (data.error||'could not stop'),6000);
  stopBtn.disabled=false; refreshRankInfo();};

// Poll for the real numbers, and tick the clock locally in between, so the
// line is visibly moving every second rather than freezing between polls.
let _rank=null, _tick=0;
setInterval(async ()=>{
  const d=await rankState(); _rank=d; _tick=0;
  if(d) paintRank(d);
}, 3000);
setInterval(()=>{ if(_rank && _rank.state==='running'){ _tick++; paintRank(_rank, _tick);} }, 1000);

rankBtn.onclick=async ()=>{
  const d=await rankState(); if(!d) return;
  const cs=rankCountries(), where=cs.length ? ` in ${cs[0]}` : '';
  if(!d.pending){ say(`Everything${where} with a description is already ranked`); return; }
  const ok=confirm(
    `Rank ${d.pending} roles${where} against your CV?\n\n`+
    `About ${d.tokens.toLocaleString()} input tokens, in ${d.batches} call(s).\n`+
    `Screening them one at a time would be about `+
    `${d.screen_tokens.toLocaleString()}.`+
    (cs.length && d.unplaced ? `\n\n${d.unplaced} more have no single country `+
      `and are left unranked.` : ''));
  if(!ok) return;
  const {ok:started,data}=await post('/api/rank',{countries:cs});
  if(!started){ say(data.error||'could not start'); return; }
  say('Ranking started. This takes a couple of minutes.');
  const t=setInterval(async ()=>{
    const s=await refreshRankInfo();
    if(s && s.state!=='running'){ clearInterval(t);
      say('Ranked. Reloading.'); setTimeout(()=>location.reload(),700);}
  },3000);
  refreshRankInfo();};

// Only rendered when the list is behind, so it is a fix offered at the moment
// the problem is visible rather than a control sitting there for ever.
const pullBtn=$('#pull');
if(pullBtn) pullBtn.onclick=async ()=>{
  pullBtn.disabled=true; pullBtn.textContent='Pulling...';
  const {ok,data}=await post('/api/pull',{});
  if(!ok){ pullBtn.disabled=false; pullBtn.textContent='Pull';
           say(data.error||'could not pull',7000); return; }
  say(data.message||'pulled');
  if(data.changed){ pullBtn.textContent='Pulled';
    // The scan reads the source list at startup, so the new boards only
    // arrive on the next scan. Say that rather than implying it is done.
    setTimeout(()=>say('Run a scan to read the boards that just arrived',6000),1200);
  } else { pullBtn.disabled=false; pullBtn.textContent='Pull'; }
};

$('#fcountry').onchange=e=>{country=e.target.value;apply();refreshRankInfo()};
$('#fcity').onchange=e=>{city=e.target.value;apply()};

async function post(url,body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  return {ok:r.ok, data:await r.json().catch(()=>({}))};}

// One place that moves a row to a status, so the page and the database
// cannot end up saying different things.
//
// Every handler used to do its own subset. Skip set row.dataset.status and
// nothing else: no pill was created, and the status select still read "new",
// so a row you had just skipped looked untouched while the database had it
// settled. The pill is only in the markup for a role that is not "new", so
// it has to be made on the way up and taken away on the way back down.
function mark(row,status){
  row.dataset.status=status;
  row.classList.toggle('settled', SETTLED.has(status));
  let pill=row.querySelector('.status');
  if(status==='new'){ if(pill) pill.remove(); }
  else{
    if(!pill){ pill=document.createElement('span');
      const head=row.querySelector('.role'); if(head) head.appendChild(pill); }
    if(pill){ pill.textContent=status; pill.className='status '+status; }}
  const sel=row.querySelector('select.setstatus');
  if(sel){ sel.dataset.current=status;
    if(sel.dataset.filled) sel.value=status;
    else sel.options[0].textContent=(status||'Status')+'\u2026'; }}

// The per-row status options are not in the HTML: see the comment where the
// select is written. Fill this one the first time it is touched, which is the
// only moment they can possibly matter.
const STATUSES=['new','interested','applied','submitted','interviewing','offer','rejected','withdrawn','skipped','closed'];
function fillStatus(sel){
  if(sel.dataset.filled) return;
  const cur=sel.dataset.current||'';
  for(const s of STATUSES){
    const o=document.createElement('option');
    o.value=s; o.textContent=s; if(s===cur) o.selected=true;
    sel.appendChild(o); }
  sel.dataset.filled='1';
  sel.value=cur; }
// What finished, after the reload that shows it.
//
// The board reloads to pick the new document up, and the reload threw away
// the only sign anything had happened. "It finished" is not the answer the
// reader wants either: they want to know whether it WORKED, which is the
// rating and the gates, and both are already rendered on the row. So the row
// is found, scrolled to, marked, and its own docs line is read back.
(function(){
  // sessionStorage here on purpose, unlike the view above. This is a
  // one-shot handoff across a single reload in one tab; in localStorage it
  // would announce a job that finished days ago the next time the board was
  // opened anywhere.
  let done=null;
  try{ done=JSON.parse(sessionStorage.getItem('job-radar:finished')||'null');
       sessionStorage.removeItem('job-radar:finished'); }catch(e){}
  if(!done || !done.length) return;
  // Runs here, at the bottom of the script, and that position is the fix.
  // On DOMContentLoaded it fired before `resort()` had reordered the rows,
  // and reordering the DOM resets the scroll: the reader was put back at the
  // top with the row unmarked, which looks exactly like nothing happening.
  // Calling it from the init block above does not work either, because that
  // block runs before this one has defined anything.
  let pending=null;
  const announce=()=>{
    const first=done[0];
    const row=document.querySelector('.row[data-uid="'+CSS.escape(first.uid)+'"]');
    if(!row){ say('Finished, but that role is not on this view.', 6000); return; }
    // The row may be hidden by the current filter, which is its own answer:
    // scrolling to something display:none does nothing and looks like the
    // click was ignored.
    if(row.hidden){ say('Finished. The role is hidden by your current filter.', 7000); return; }
    row.classList.add('justdone');
    // The browser restores the previous scroll position on a reload, and it
    // does it AFTER this script runs, so scrolling here was undone a frame
    // later and the reader stayed at the top. Told not to restore, and the
    // scroll deferred a frame so it lands after the last layout of the load.
    try{ history.scrollRestoration='manual'; }catch(e){}
    // No automatic scroll. It was tried and it does not survive this page:
    // the browser restores the old scroll position after the script runs, and
    // `content-visibility` means the rows above have never been laid out, so
    // the document reflows under the jump as their real heights resolve and
    // the row drifts off screen again. Measured at 883px above the viewport a
    // moment after landing on it.
    //
    // So the toast carries the jump instead. A scroll the reader asks for
    // happens after layout has settled and lands every time, and it does not
    // yank the page out from under someone who was already reading something
    // else. Same reasoning as the boot overlay: better to say plainly what
    // happened than to move things and hope.
    pending=row;
    const docs=row.querySelector('.docs');
    const title=(row.querySelector('.role a')||{}).textContent||'the role';
    const name={screen:'Screening',cv:'CV',cover_letter:'Cover letter'}[first.kind]||first.kind;
    // Read back what the row itself says, rather than a second copy of the
    // outcome that could disagree with it.
    const detail=docs ? docs.textContent.replace(/\s+/g,' ').trim() : '';
    say(name+' done for '+title.slice(0,48)+(detail?' \u2014 '+detail:'')
        +'  \u2014  click to show it', 14000);
  };
  // The toast is the control. It stays up long enough to be clicked and
  // clears itself either way.
  const toast=$('#toast');
  if(toast){ toast.style.cursor='pointer'; toast.title='Jump to the role';
    toast.addEventListener('click', ()=>{
      if(!pending) return;
      // Filtered to, not scrolled to. `scrollIntoView` on a board this tall
      // computes against `content-visibility` estimates for every row above
      // and lands in the wrong place; hiding the rest puts the role at the
      // top of the list with no arithmetic at all. Any tab or chip puts the
      // board back, because `apply()` owns `hidden` and will overwrite this
      // on its next pass.
      for(const r of document.querySelectorAll('.row')) r.hidden = r!==pending;
      $('#empty').hidden=true; $('#list').hidden=false;
      window.scrollTo(0,0);
      toast.classList.remove('show');
      say('Showing that one role. Pick a tab to go back to the board.', 8000); }); }
  announce();
})();

// One line saying what the queue is doing, at the top of the page.
//
// The per-row note only helps if you already know which row to look at, and
// a bulk screen of nine roles puts a note on nine rows you cannot all see at
// once. So the count lives beside the rank line, where "is anything still
// happening" is answered without scrolling.
function paintJobs(d){
  const el=$('#jobsinfo'); if(!el) return;
  const live=(d.jobs||[]).filter(j=>j.state==='running'||j.state==='pending');
  if(!live.length){ el.textContent=''; return; }
  const running=live.filter(j=>j.state==='running').length;
  const queued=live.length-running;
  const kinds=[...new Set(live.map(j=>j.kind))];
  const name=kinds.length===1
    ? ({screen:'screening',cv:'CV',cover_letter:'cover letter'}[kinds[0]]||kinds[0])
    : 'jobs';
  let s=running+' '+name+(running===1?'':'s')+' running';
  if(queued) s+=', '+queued+' queued';
  // The typical is per kind and only useful when there is one kind in flight.
  const usual=(d.typical||{})[kinds[0]];
  if(kinds.length===1 && usual && live.length>1){
    const rounds=Math.ceil(live.length/Math.max(running,1));
    s+=', roughly '+hhmm(usual*rounds)+' left';
  }
  el.textContent=s; }

// ------------------------------------------------------- job progress
// A drafting run is a draft, a set of quality gates and up to two revisions,
// which on this machine is a median of about eight minutes. The row showed a
// disabled button and nothing else for all of it, so the only way to tell a
// working job from a dead one was to read the process table. That is the same
// failure as a page that paints nothing for a second, except eight minutes of
// it, and the reader's remedy is worse: they kill a job that was fine.
//
// The typical is the median of the last ten completed runs of that kind on
// THIS machine, computed server-side, and it is absent until there are at
// least two to go on. A number in the source would be wrong within a release
// and wrong in the expensive direction: told to expect two minutes at minute
// six, you conclude it has hung.
const KINDNAME={screen:'Screening',cv:'Drafting CV',cover_letter:'Drafting cover letter'};
function hhmm(s){
  s=Math.max(0,Math.round(s));
  const m=Math.floor(s/60), r=s%60;
  return m ? m+'m '+String(r).padStart(2,'0')+'s' : r+'s';}
// Server clock against server timestamps. The browser's clock is not the
// server's, and on a laptop that has slept they can be minutes apart, which
// would show a job that started in the future.
function progressOn(row, j, typical, now){
  let el=row.querySelector('.jobprog');
  if(!el){ el=document.createElement('div'); el.className='jobprog';
           const acts=row.querySelector('.acts');
           row.insertBefore(el, acts || row.querySelector('.err') || null); }
  if(j.state==='pending'){
    el.textContent=(KINDNAME[j.kind]||j.kind)+' queued, waiting for a free slot';
    return; }
  const t0=Date.parse((j.started_at||'').replace(' ','T'));
  const t1=Date.parse((now||'').replace(' ','T'));
  const secs=(isNaN(t0)||isNaN(t1)) ? null : (t1-t0)/1000;
  const usual=typical[j.kind]||0;
  let s=(KINDNAME[j.kind]||j.kind);
  if(secs!==null) s+=', '+hhmm(secs);
  if(usual) s+=', usually about '+hhmm(usual);
  // Only once it is genuinely over, and said plainly rather than as a
  // warning: a long run is not a broken one, and the point of the line is
  // that the reader can tell the difference themselves.
  if(usual && secs!==null && secs > usual*1.5) s+=' (running long, still going)';
  el.textContent=s; }
function progressOff(row){
  const el=row.querySelector('.jobprog'); if(el) el.remove(); }

// ---------------------------------------------------------------- bulk
// Screening reads one advert properly and costs about SCREEN_TOKENS input
// tokens a role, which is why this tool ranks a whole board for roughly the
// price of screening twenty of it. A bulk button is therefore the one control
// here that can spend real money on a mis-click, so it says the number and
// the cost and asks, every time, however many are picked.
const SCREEN_TOKENS=%SCREEN_TOKENS%, BULK_LIMIT=%BULK_LIMIT%;
const picked=new Set();
function fmt(n){return n.toLocaleString();}
function paintBulk(){
  const bar=$('#bulk'); if(!bar) return;
  const n=picked.size;
  bar.hidden = n===0;
  if(!n) return;
  $('#bulkn').textContent = n+' selected';
  const over = n>BULK_LIMIT;
  $('#bulkcost').innerHTML = over
    ? '<span class="warn">more than '+BULK_LIMIT+' at once is refused</span>'
    : 'about '+fmt(n*SCREEN_TOKENS)+' input tokens to screen, '
      +fmt(n*SCREEN_TOKENS)+' more if you draft CVs too';
  $('#bulkscreen').disabled = over;
  $('#bulkcv').disabled = over; }
document.addEventListener('change', e=>{
  const box=e.target.closest && e.target.closest('input.sel'); if(!box) return;
  const row=box.closest('.row'); if(!row) return;
  box.checked ? picked.add(row.dataset.uid) : picked.delete(row.dataset.uid);
  paintBulk(); });
function clearPicked(){
  picked.clear();
  document.querySelectorAll('input.sel:checked').forEach(b=>b.checked=false);
  paintBulk(); }
// Only the rows the reader can actually see. "Select all" over a filtered
// board that quietly took the four thousand hidden ones with it is the
// expensive version of the mistake this whole file is written against.
function pickVisible(){
  let n=0;
  for(const r of document.querySelectorAll('.row')){
    if(r.hidden) continue;
    const b=r.querySelector('input.sel'); if(!b) continue;
    b.checked=true; picked.add(r.dataset.uid); n++; }
  paintBulk(); return n; }
async function bulkGenerate(kind){
  const uids=[...picked];
  if(!uids.length) return;
  const cost=fmt(uids.length*SCREEN_TOKENS);
  const what = kind==='screen' ? 'Screen' : 'Draft a CV for';
  if(!confirm(what+' '+uids.length+' role'+(uids.length===1?'':'s')+'?\n\n'
      +'Roughly '+cost+' input tokens, spent through your Claude subscription. '
      +'At most %MAX_RUNNING% run at once and the rest queue.')) return;
  const {ok,data}=await post('/api/generate/bulk',{kind,uids});
  if(!ok){ say(data.error||'could not start'); return; }
  const started=(data.queued||data.started||[]).length, skipped=(data.skipped||[]);
  // Names, not a count. "3 skipped" on a shortlist means opening every row to
  // find out which three and why.
  let msg=started+' queued'
    + (data.running ? ', '+data.running+' running now' : '');
  if(skipped.length){
    const why=skipped[0].why||'refused';
    msg += ', '+skipped.length+' skipped ('+why
        +(skipped.length>1?' and others':'')+')'; }
  say(msg, 6000);
  clearPicked();
  if(started) poll(); }
(function(){
  const s=$('#bulkscreen'), c=$('#bulkcv'), x=$('#bulkclear');
  if(s) s.onclick=()=>bulkGenerate('screen');
  if(c) c.onclick=()=>bulkGenerate('cv');
  if(x) x.onclick=clearPicked; })();
// A keyboard route, because ticking forty boxes by hand is the thing this is
// meant to replace. Not Ctrl-A: that is select-the-text and taking it would
// break copying a title out of the board.
document.addEventListener('keydown', e=>{
  if(e.key==='a' && (e.metaKey||e.ctrlKey) && e.shiftKey){
    e.preventDefault(); say(pickVisible()+' selected'); }
  if(e.key==='Escape' && picked.size) clearPicked(); });

// The same argument as fillStatus, one level up: the whole action bar is
// absent from every row past EAGER_ROWS and built here the moment the row is
// touched. Kept in step with the Python that writes the eager ones by
// tests/test_dashboard_lazy_actions.py, because two renderers of one control
// is a drift that has already cost this dashboard a working status menu.
function fillActs(row){
  if(!row || !row.dataset.lazyacts) return;
  delete row.dataset.lazyacts;
  const cv=row.dataset.hascv==='1', settled=row.dataset.settled==='1';
  const st=row.dataset.status||'';
  const d=document.createElement('div'); d.className='acts';
  const btn=(html)=>{const s=document.createElement('span');s.innerHTML=html;return s.firstChild;};
  // Whether a document already exists comes from the row's data-open-*
  // attributes, written from the artifacts table. Not from the rendered docs
  // line: the screening is shown inline in a <details> and has no link there
  // at all, so reading the markup decided no screening existed and offered a
  // button that would pay to run it a second time.
  const pair=(kind,label,made,attr,cls)=>{
    // `attr` is the suffix from DOC_ATTR, capitalised. Not derived from the
    // kind: deriving it is what silently lost the cover letter's buttons.
    const href=row.dataset['open'+attr[0].toUpperCase()+attr.slice(1)];
    if(!href) return [btn('<button class="'+(cls||'')+'" data-gen="'+kind+'">'+label+'</button>')];
    const dl=document.createElement('a');
    dl.className='btn '+(cls||''); dl.href=href.replace('/open?','/download?');
    dl.setAttribute('download','');
    dl.title='Save it to your Downloads folder, where the browser\'s upload '
            +'dialog opens';
    dl.textContent='Download '+made;
    const open=document.createElement('a');
    open.className='btn'; open.href=href; open.title='Show it in Finder';
    open.textContent='Open';
    const out=[dl, open];
    // See the Python beside this: a screening reads an advert that has not
    // changed, so there is nothing to gain from a second one.
    if(kind!=='screen') out.push(btn('<button data-gen="'+kind+'" data-redraft="1" '
      +'title="Draft it again. This spends tokens.">Redraft</button>'));
    return out; };
  for(const el of pair('screen','Screen','screening','screen','primary')) d.appendChild(el);
  for(const el of pair('cv','CV','CV','cv')) d.appendChild(el);
  if(cv){ for(const el of pair('cover_letter','Cover letter','letter','letter')) d.appendChild(el); }
  else d.appendChild(btn('<button disabled title="Draft the CV first: the letter is checked against it for repeated phrasing">Cover letter</button>'));
  const a=document.createElement('a'); a.className='btn'; a.href=row.dataset.url||'#';
  a.target='_blank'; a.rel='noopener'; a.dataset.apply='1'; a.textContent='Apply';
  d.appendChild(a);
  d.appendChild(btn('<button data-status="skipped">Skip</button>'));
  if(settled) d.appendChild(btn('<button data-status="interested">Unskip</button>'));
  const sel=document.createElement('select');
  sel.className='setstatus'; sel.setAttribute('aria-label','Set status');
  sel.dataset.current=st;
  const ph=document.createElement('option'); ph.value=''; ph.textContent=(st||'Status')+'\u2026';
  sel.appendChild(ph); d.appendChild(sel);
  d.appendChild(btn('<button data-note="1" title="Add or edit a note">Note</button>'));
  const err=row.querySelector('.err');
  row.insertBefore(d, err || null); }
document.addEventListener('pointerover', e=>{
  const row=e.target.closest && e.target.closest('.row'); if(row) fillActs(row); }, true);
document.addEventListener('focusin', e=>{
  const row=e.target.closest && e.target.closest('.row'); if(row) fillActs(row); });
// A click that lands before the hover handler has run still has to work: a
// touch screen has no hover at all, and a row whose buttons were never built
// would swallow the tap in silence.
document.addEventListener('pointerdown', e=>{
  const row=e.target.closest && e.target.closest('.row'); if(row) fillActs(row); }, true);

document.addEventListener('pointerdown', e=>{
  const sel=e.target.closest('select.setstatus'); if(sel) fillStatus(sel); }, true);
document.addEventListener('focusin', e=>{
  const sel=e.target.closest('select.setstatus'); if(sel) fillStatus(sel); });

// The status select was rendered with all ten statuses and never wired to
// anything, so picking "rejected" looked like it worked, changed nothing, and
// reverted on the next refresh. It is a change event, not a click, which is
// why the click handler below never saw it.
document.addEventListener('change', async e=>{
  const sel=e.target.closest('select.setstatus'); if(!sel) return;
  const row=sel.closest('.row'); if(!row) return;
  const status=sel.value; if(!status) return;
  const prev=row.dataset.status;
  const {ok,data}=await post('/api/status',{uid:row.dataset.uid,status});
  if(!ok){ sel.value=prev||''; say(data.error||'could not save'); return; }
  mark(row,status);
  say('Marked '+status+(SETTLED.has(status)?'. It will not come back.':''));
  apply();
});

document.addEventListener('click', async e=>{
  // Document links reveal the file in Finder. Without this they navigate the
  // tab to a raw JSON body and you lose the dashboard.
  const doc=e.target.closest('.docs a');
  if(doc){ e.preventDefault();
    const r=await fetch(doc.getAttribute('href'));
    const d=await r.json().catch(()=>({}));
    say(d.ok?'Revealed in Finder':(d.error||'could not open it'));
    return;}

  const row=e.target.closest('.row'); if(!row) return;
  const uid=row.dataset.uid;

  // Scoped to the button. The row wrapper also carries data-status, for
  // filtering, so an unscoped closest() walked up from every button and hit
  // the row first: Screen, CV, Cover letter and Apply all silently posted
  // status "new" and returned before reaching their own branch. Skip worked
  // only because its own button carries the attribute, so closest stopped
  // there -- which is why testing Skip alone said the buttons were fine.
  const st=e.target.closest('button[data-status]');
  if(st){ const status=st.dataset.status;
    const {ok,data}=await post('/api/status',{uid,status});
    if(!ok){say(data.error||'could not save');return}
    mark(row,status);
    say(status==='skipped'?'Skipped. It will not come back.':'Marked '+status);
    apply(); return;}

  if(e.target.closest('[data-apply]')){
    // Apply is also just the link to the advert, and it posted "applied"
    // unconditionally. So re-opening the board for a role you were already
    // interviewing for traded the interview for an application, in the
    // database, with the pill on screen still reading "interviewing" so
    // there was nothing to notice. That is the same trade store.PROGRESS
    // exists to stop a merge making, made by the button pressed most often.
    // It only ever moves a role forward now; the link opens either way.
    const cur=row.dataset.status||'new';
    if((PROGRESS[cur]||0) >= PROGRESS.applied){
      say('Opening the job board. Still marked '+cur+'.'); return;}
    const {ok,data}=await post('/api/status',{uid,status:'applied'});
    if(!ok){ say(data.error||'could not save'); return; }
    mark(row,'applied'); say('Marked applied, opening the job board');
    apply(); return;}

  if(e.target.closest('[data-note]')){
    const cur=row.querySelector('.rownote');
    const note=prompt('Note for this role:', cur?cur.textContent:'');
    if(note===null) return;
    const {ok,data}=await post('/api/status',
      {uid,status:row.dataset.status,note:note});
    if(!ok){say(data.error||'could not save');return}
    say('Note saved'); location.reload(); return;}

  const gen=e.target.closest('[data-gen]');
  if(gen && !gen.disabled){
    const kind=gen.dataset.gen;
    // Redraft is the only one of these that can throw away work already paid
    // for, so it is the only one that asks. Everything else here is either
    // free or is the first time.
    if(gen.dataset.redraft){
      const name={screen:'screening',cv:'CV',cover_letter:'cover letter'}[kind]||kind;
      if(!confirm('Draft this '+name+' again?\n\nThere is one on file already. '
                  +'This spends tokens and replaces it.')) return; }
    const {ok,data}=await post('/api/generate',{uid,kind});
    const err=row.querySelector('.err');
    if(!ok){ err.hidden=false; err.textContent=data.error||'could not start';
             say(data.error||'could not start',5000); return;}
    err.hidden=true;
    gen.classList.add('busy'); gen.disabled=true;
    say(kind==='screen'?'Screening. Takes a few seconds.'
       :'Drafting. This takes a few minutes; the row updates itself.');
    poll();}
});

let polling=null;
// Anything that finished during THIS polling session. `/api/jobs` only
// reports completions from the last two minutes, so a long queue can empty
// with its last success already outside that window, and the reload that
// shows the documents would never fire.
let sawFinished=[];
async function poll(){
  if(polling) return;
  polling=setInterval(async ()=>{
    const r=await fetch('/api/jobs'); if(!r.ok) return;
    const d=await r.json();
    const busy=new Set(d.jobs.filter(j=>j.state==='pending'||j.state==='running')
                            .map(j=>j.uid+':'+j.kind));
    let anyBusy=false;
    for(const row of document.querySelectorAll('.row')){
      const uid=row.dataset.uid;
      row.querySelectorAll('[data-gen]').forEach(b=>{
        const on=busy.has(uid+':'+b.dataset.gen);
        if(on) anyBusy=true;
        // toggle() has already removed the class by the time we test it, so
      // asking whether it is still busy always said no and the button stayed
      // disabled for ever once a job finished.
      const was=b.classList.contains('busy');
      b.classList.toggle('busy',on);
      if(was && !on) b.disabled=false;});
    }
    // Show the newest job per role, not every failure ever returned. A retry
    // that is already running was being covered by the error from the attempt
    // it replaced, so the row said "generation failed" while the spinner span.
    // Nothing cleared the message either, so once shown it stayed until the
    // page was reloaded.
    const newest=new Map();
    for(const j of d.jobs){
      const prev=newest.get(j.uid);
      if(!prev || j.id>prev.id) newest.set(j.uid,j);}
    for(const row of document.querySelectorAll('.row')){
      const e=row.querySelector('.err'); if(!e) continue;
      const j=newest.get(row.dataset.uid);
      if(j && j.state==='failed'){
        e.hidden=false; e.textContent='Generation failed: '+(j.error||'unknown');
        progressOff(row);
      }else if(j && (j.state==='running'||j.state==='pending')){
        e.hidden=true; e.textContent='';
        progressOn(row, j, d.typical||{}, d.now);
      }else if(j){
        e.hidden=true; e.textContent=''; progressOff(row);}}
    paintJobs(d);
    const finished=d.jobs.filter(j=>j.state==='done');
    // Only once the queue is EMPTY. Reloading on every completion was fine
    // when one click meant one job and is unusable with a queue behind it:
    // ten screens is ten reloads, each one throwing away the scroll position
    // and re-closing the screening you had just opened to read. Which is
    // exactly how this was found.
    for(const j of finished){
      if(!sawFinished.some(x=>x.uid===j.uid && x.kind===j.kind))
        sawFinished.push({uid:j.uid, kind:j.kind}); }
    const busyStill=d.jobs.some(j=>j.state==='running'||j.state==='pending');
    if(busyStill && sawFinished.length){
      // Say what landed, so the wait is legible without a reload.
      say(sawFinished.length+' finished, '
          +d.jobs.filter(j=>j.state!=='done').length
          +' to go. The board refreshes when the queue is empty.', 4000);
    }
    if(sawFinished.length && !busyStill){ clearInterval(polling); polling=null;
      // Remember WHICH role finished across the reload. Without this the
      // page came back with a toast already gone, no scroll position, and the
      // result sitting somewhere in four thousand rows: the job had worked
      // and there was no way to tell without hunting for the row.
      try{ sessionStorage.setItem('job-radar:finished',
             JSON.stringify(sawFinished)); }catch(e){}
      say('Done. Reloading to show the documents.');
      setTimeout(()=>location.reload(),900); return;}
    if(!anyBusy && d.jobs.length===0){clearInterval(polling);polling=null;}
  },2500);}

// Poll whenever something is in flight, not only when a busy BUTTON is on the
// page. Past EAGER_ROWS a row has no buttons until it is touched, so a job on
// row 500 showed no progress at all and the tab looked idle while it ran.
(async ()=>{
  if(document.querySelector('.acts button.busy')) return poll();
  try{ const r=await fetch('/api/jobs'); if(!r.ok) return;
       const d=await r.json();
       if((d.jobs||[]).some(j=>j.state==='running'||j.state==='pending')) poll();
  }catch(e){}
})();
"""


def _rows(con):
    """Roles seen recently, plus every role you have acted on.

    Filtering on the last scan alone made applied and interviewing roles
    disappear the moment a posting closed, a source was rate-limited, or a
    `--limit` run happened -- taking their status and their generated
    documents with them, with no other view of them anywhere. Filtering on
    the newest date alone had the same effect for everything else: one
    limited run emptied the board.
    """
    return con.execute("""
        SELECT r.*, COALESCE(s.status,'new') AS status, COALESCE(s.note,'') AS note
        FROM roles r LEFT JOIN role_state s ON s.uid = r.uid
        WHERE """ + store.LIVE_SQL + " AND " + store.ACTIONABLE_SQL + """
           OR COALESCE(s.status,'new') <> 'new'
           OR r.uid IN (SELECT DISTINCT uid FROM artifacts)
        ORDER BY r.score DESC, r.company COLLATE NOCASE
    """).fetchall()


def _floor_is_set(rows) -> bool:
    """Whether the config that produced these rows carries a salary floor.

    The footer used to assert one unconditionally: "Roles with a stated salary
    below your floor are hidden", to every reader, including the majority who
    have no floor. `floor: null` is what the setup wizard writes when somebody
    answers "I do not know", so that sentence was false on the first dashboard
    a new user ever opened, and it was the only thing on the page explaining
    why a role might be missing.

    `render` is handed a database connection and a currency and nothing else,
    so the floor is not available to ask for directly -- `serve.py` reads the
    config and passes only `salary_currency` through. But the rows carry the
    answer anyway. `screen.apply_salary` files whatever `salary.clears_floor`
    hands back as a flag on the role, and `clears_floor` returns an empty
    reason and files nothing at all when there is no floor. Every reason it
    can produce for a role it KEEPS names the floor or is the bare string
    "unconfirmed salary", and nothing else in the tool writes either one into
    `flags`. So a flag of that shape is proof a floor exists.

    The inference only runs one way, which is the safe way. Present means
    certain; absent means "no evidence", which happens for a real floor only
    when every stored role has a confirmed salary in the floor's own currency.
    That case loses a true sentence, which is a great deal better than the old
    behaviour of showing a false one to everybody.
    """
    for r in rows:
        try:
            flags = _flags(r)
        except (TypeError, ValueError):
            continue
        if not isinstance(flags, list):
            continue
        for f in flags:
            if f == "unconfirmed salary" or (isinstance(f, str) and "floor in " in f):
                return True
    return False


def _empty_state(con, rows) -> str:
    """The panel shown when the list has nothing in it.

    It said "Nothing matches those filters." in both of the two situations
    that produce it, and only one of them involves a filter.

    With no rows at all the page loads with nothing selected and no filter
    hiding anything, so the sentence is false and the next step it implies --
    go and change a filter -- is a dead end. What the reader needs there is
    whether a scan has ever run, which the database knows: `store` counts them
    under `runs`, and zero is the state a fresh clone is in between `setup`
    and the first `scan`.

    With rows on the page the filters really are the only way to empty it, and
    the tab strip defaults to Open rather than All, so a board whose roles are
    all settled lands here on the first load with a filter genuinely applied.
    That sentence was right; it just never said how to get back.
    """
    if rows:
        return ('<div class="empty" id="empty" hidden><p>Nothing matches those '
                'filters. Choose <b>All</b> above, and clear any sector, '
                'working pattern, country or city you picked.</p></div>')
    runs = int(store.get_meta(con, "runs", "0") or 0)
    if not runs:
        return ('<div class="empty" id="empty"><p><b>No scan has run yet.</b> '
                'This board is filled by <code>job-radar scan</code>; run it '
                'in a terminal and reload this page. The first one takes about '
                'an hour, and roles appear as it goes.</p></div>')
    return ('<div class="empty" id="empty"><p><b>The last scan stored no roles.</b> '
            'Nothing here is filtered out: there is nothing to filter. The scan '
            'itself printed where every posting went, which is the thing to '
            'read.</p><p>Most often this is the titles. Check '
            '<code>titles.include</code> matches how postings are actually '
            'worded, and add employers yourself with '
            '<code>job-radar discover &lt;company&gt; --add</code>.</p></div>')


def render(con, home_currency: str = "") -> str:
    rows = _rows(con)
    # Keyed on the scan date rather than the run number, so a second scan the
    # same day does not empty the New tab.
    run = store.new_today(con)
    arts = {}
    for a in con.execute("SELECT * FROM artifacts ORDER BY id"):
        arts.setdefault(a["uid"], {})[a["kind"]] = dict(a)
    live = {j["uid"]: dict(j) for j in con.execute(
        "SELECT * FROM jobs WHERE state IN ('pending','running')")}

    total = len(rows)
    paid = sum(1 for r in rows if r["salary_confirmed"])
    settled = sum(1 for r in rows if r["status"] in store.SETTLED)
    # Said in the header, not only on the chip. The contract count is the
    # answer to "is there anything in this market at all today", and a number
    # that only exists on a facet is a number you have to go looking for.
    contract_n = sum(1 for r in rows if _emp(r) == employment.CONTRACT)

    # "What changed since yesterday" is the whole point of running this daily,
    # and the count was previously only ever a line of stdout that scrolled
    # away. first_run is in the database already; this surfaces it.
    fresh = sum(1 for r in rows if r["uid"] in run)
    _new_count = f'<span class="n">{fresh}</span>' if fresh else ""

    # Applications you are actually in. The board is mostly a list of things
    # you have not done anything about; the handful you have is the part with
    # a deadline attached, and it was scattered among three hundred rows.
    inflight = sum(1 for r in rows if r["status"] in store.IN_FLIGHT)
    _live_count = f'<span class="n">{inflight}</span>' if inflight else ""

    # Rejections and withdrawals, which every other view hides. Worth being
    # able to look at on purpose: it is the record of what you actually went
    # for, and it is the only place to notice a pattern in what comes back.
    # Skipped roles are not here -- you never applied to those.
    shut = sum(1 for r in rows if r["status"] in store.CLOSED_OUT)
    _closed_count = f'<span class="n">{shut}</span>' if shut else ""

    # Roles judged a real fit against the CV, not merely eligible against the
    # filters. -1 means unranked, which is not the same as bad.
    good = sum(1 for r in rows if (r["fit"] or -1) >= 70)
    _fit_count = f'<span class="n">{good}</span>' if good else ""

    # When the bundled source list was last checked against reality. Shown
    # because nothing else tells you: the weekly validation and growth jobs
    # run upstream, so a clone's list freezes on the day it was cloned and a
    # fork only ever prunes its own. A checkout months behind quietly loses
    # boards as they migrate and looks exactly as healthy as a fresh one.
    from .. import sources as _src
    age = _src.age_days()
    if age is None:
        _sync = ('<span class="sync warn" title="sources.json carries no date">'
                 'sources: never synced</span>'
                 '<button id="pull" type="button">Pull</button>')
    else:
        when = "today" if age == 0 else ("yesterday" if age == 1
                                         else f"{age} days ago")
        # Upstream validates weekly, so eight days is one missed cycle.
        cls = "sync warn" if age > 8 else "sync"
        tip = ("Run `git pull` to get boards that have moved and employers "
               "added since." if age > 8 else "Up to date with the weekly "
               "upstream check.")
        _sync = (f'<span class="{cls}" title="{_h.escape(tip, quote=True)}">'
                 f'sources synced {when}</span>'
                 + ('<button id="pull" type="button" title="git pull --ff-only '
                    'in this checkout">Pull</button>' if age > 8 else ''))

    # Everything else that ages, in the one place the reader already looks.
    #
    # The sources line above has said its own age for a while, and that was
    # the only thing on this page that ever admitted to being old. Three
    # scheduled jobs were then found to have been failing for weeks: the
    # weekly validation dying on its first templated source, the seed rebuild
    # dropping 287,219 roles at the upload, and two scans killed part way. The
    # board looked exactly the same throughout, because a stale artefact
    # renders identically to a fresh one.
    #
    # Only shown when something is actually late. A permanent green badge
    # saying everything is fine becomes furniture within a week and then it is
    # not read at all.
    from .. import freshness as _fresh
    _stale_items = [i for i in _fresh.report(con) if not i.ok
                    and i.key != "sources"]     # sources has its own badge
    if not rows:
        # A checkout that has never scanned already says so, at length, in the
        # empty state. Two messages about the same absence is one too many.
        _stale_items = []
    if _stale_items:
        _worst = _fresh.worst(_stale_items)
        _bits = "; ".join(
            f"{i.label.lower()} {i.days} days ago" if i.days is not None
            else f"{i.label.lower()} never"
            for i in _stale_items)
        _tip = " | ".join(f"{i.says()}. {i.fix}" for i in _stale_items if i.fix)
        _stale = (f'<span class="sync agecheck {_worst}" '
                  f'title="{_h.escape(_tip, quote=True)}">{_h.escape(_bits)}</span>')
    else:
        _stale = ""

    sec = Counter((r["sector"] or "other") for r in rows)
    chips = "".join(
        f'<button aria-pressed="false" data-sec="{_h.escape(s, quote=True)}">'
        f'{_h.escape(_SECTORS.get(s, s.title()))}<span class="n">{n}</span></button>'
        for s, n in sec.most_common())
    mc = Counter((r["work_mode"] or "unstated") for r in rows)
    modes = "".join(
        f'<button aria-pressed="false" data-mode="{m}">{_MODES[m]}'
        f'<span class="n">{mc[m]}</span></button>'
        for m in ("remote", "hybrid", "office", "unstated") if mc.get(m))
    # Employment type, as its own row of chips rather than folded into the
    # working-pattern one. They are different questions -- remote is where you
    # sit, contract is what you are -- and a reader who wants six months of
    # interim work at a day rate is not asking the same thing as a reader who
    # wants to work from home.
    #
    # "Not stated" is a chip in its own right and is deliberately not merged
    # into Permanent. Most employers say nothing, and folding silence into
    # "permanent" would hide the larger half of the contract market behind a
    # label that asserts the opposite of what is known.
    ec = Counter(_emp(r) for r in rows)
    emps = "".join(
        f'<button aria-pressed="false" data-emp="{e}">{_EMP_LABELS[e]}'
        f'<span class="n">{ec[e]}</span></button>'
        for e in (employment.CONTRACT, employment.PERMANENT, employment.UNSTATED)
        if ec.get(e))
    # data-label so the count can be rewritten in the browser against the rows
    # the current tab actually shows, without losing the name.
    cc = Counter((r["country"] or "unknown") for r in rows)
    countries = '<option value="">All countries</option>' + "".join(
        f'<option value="{_h.escape(c, quote=True)}" '
        f'data-label="{_h.escape(c, quote=True)}">{_h.escape(c)} ({n})</option>'
        for c, n in cc.most_common())
    cty = Counter(r["city"] for r in rows if r["city"])
    cities = '<option value="">All cities</option>' + "".join(
        f'<option value="{_h.escape(c, quote=True)}" '
        f'data-label="{_h.escape(c, quote=True)}">{_h.escape(c)} ({n})</option>'
        for c, n in sorted(cty.items(), key=lambda x: (-x[1], x[0])))

    # Your currency, the one `salary.floor` is written in. `bySalary` only
    # compares figures inside one currency, because `salary.clears_floor`
    # refuses to compare across them and the sort has no business being
    # braver than the filter.
    #
    # This used to be the currency most of the BOARD's stated salaries were
    # in, which is not the same thing and inverted the sort for anyone whose
    # results are mostly foreign. On a GBP floor over a board holding 10 USD,
    # 8 GBP and 7 EUR figures, USD won the vote, so every row already stamped
    # "salary in USD, floor in GBP, not compared" sorted ABOVE the sterling
    # rows that HAD been compared -- the sort contradicting the caveat printed
    # on the same row, which is the exact fault the grouping was added to fix.
    #
    # The board's own modal currency stays as the fallback, for a config with
    # no currency set at all.
    home_cur = (home_currency or "").upper()
    if not home_cur:
        _cur = Counter(r["salary_currency"] for r in rows
                       if r["salary_confirmed"] and r["salary_currency"])
        home_cur = _cur.most_common(1)[0][0] if _cur else ""
    prelude = (f"const HOME_CUR={json.dumps(home_cur)},"
               f"PROGRESS={json.dumps(store.PROGRESS)};")

    _empty = _empty_state(con, rows)
    _floor = ("Roles with a stated salary below your floor are hidden. "
              if _floor_is_set(rows) else "")

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job radar</title>{_FAVICON}<style>{_CSS}{_EXTRA_CSS}</style></head><body>
<div class="boot" id="boot" role="status" aria-live="polite">
  <div class="boot-spin" aria-hidden="true"></div>
  <p>Loading {total} roles</p>
  <p class="boot-sub">The board is built in your browser, so this is the slow bit,
     not the server.</p>
</div>
<script>
// The overlay must not outlive a script that failed. Both of these clear it
// even if the main script below never runs: a page that says "Loading" for
// ever is a worse answer than a page that looks broken, because the reader
// waits instead of reloading.
addEventListener('DOMContentLoaded',()=>{{setTimeout(()=>{{
  const b=document.getElementById('boot'); if(b) b.remove(); }},50);}});
setTimeout(()=>{{const b=document.getElementById('boot'); if(b) b.remove();}},8000);
</script>
<div class="wrap">
<header>
  <div class="brand">{_MARK}<span>job radar</span></div>
  <h1>{total} roles worth a look</h1>
  <p class="sub"><b>{paid}</b> with a salary &middot; <b>{contract_n}</b> contract or interim
     &middot; <b>{settled}</b> settled &middot;
     live from the database, so anything you click sticks</p>
</header>
<div class="seg" role="tablist" aria-label="Filter roles">
  <button role="tab" aria-selected="false" data-f="all">All</button>
  <button role="tab" aria-selected="false" data-f="new">New{_new_count}</button>
  <button role="tab" aria-selected="false" data-f="fit">Best fit{_fit_count}</button>
  <button role="tab" aria-selected="false" data-f="live">In flight{_live_count}</button>
  <button role="tab" aria-selected="false" data-f="closed">Closed{_closed_count}</button>
  <button role="tab" aria-selected="true"  data-f="open">Open</button>
  <button role="tab" aria-selected="false" data-f="pay">Salary shown</button>
</div>
<div class="actions"><button id="rank" type="button">Rank against my CV</button>
  <button id="rankstop" type="button" hidden>Stop</button>
  <span id="rankinfo"></span><span id="jobsinfo"></span>{_sync}{_stale}</div>
<div class="chips" role="group" aria-label="Filter by sector">{chips}</div>
<div class="chips" role="group" aria-label="Filter by working pattern">{modes}</div>
<div class="chips emps" role="group" aria-label="Filter by employment type">{emps}</div>
<div class="selects">
  <label><span>Sort</span><select id="fsort" aria-label="Sort order">
    <option value="rank">Priority</option>
    <option value="fit">Fit against my CV</option>
    <option value="salary">Salary</option>
    <option value="new">Newest</option>
  </select></label>
  <label><span>Country</span><select id="fcountry" aria-label="Country">{countries}</select></label>
  <label><span>City</span><select id="fcity" aria-label="City">{cities}</select></label>
</div>
<div class="list" id="list">{"".join(_row(r, arts.get(r["uid"], {}), live.get(r["uid"]), run, eager=i < EAGER_ROWS) for i, r in enumerate(rows))}</div>
<div class="bulk" id="bulk" hidden>
  <div><b id="bulkn">0 selected</b>
    <span class="cost" id="bulkcost"></span></div>
  <div class="acts" style="margin:0">
    <button id="bulkscreen" class="primary" type="button">Screen selected</button>
    <button id="bulkcv" type="button">Draft CVs</button>
    <button id="bulkclear" type="button">Clear</button>
  </div>
</div>
{_empty}
<footer>{_floor}Nothing is generated
unless you click for it.</footer>
</div><div class="toast" id="toast"></div>
<script>{prelude}{_js()}</script></body></html>"""


# What the row says about employment type, defended against a database made
# before the column existed.
#
# `sqlite3.Row` raises IndexError rather than returning None for a column that
# is not in the SELECT, and the dashboard is opened against whatever database
# the reader has. An unknown value is reported as "unstated", which is the one
# answer that is always true when nothing has classified the role -- never
# "permanent", which would be this tool inventing a fact about somebody's job.
_EMP_LABELS = {
    employment.PERMANENT: "Permanent",
    employment.CONTRACT: "Contract or interim",
    employment.UNSTATED: "Not stated",
}


def _emp(row) -> str:
    try:
        v = row["employment"]
    except (IndexError, KeyError, TypeError):
        return employment.UNSTATED
    return v if v in employment.VALUES else employment.UNSTATED


def _flags(row) -> list:
    """The role's flags, or none of them.

    `store.py` already guards this field and this did not, so a row with a
    malformed `flags` value would raise out of the page handler and 500 the
    whole dashboard rather than losing one role's labels. Nothing has ever
    written a bad value, which is exactly why it is worth a guard: the cost of
    being wrong is the entire board, and the cost of being careful is four
    lines.
    """
    import json as _json
    try:
        out = _json.loads(row["flags"] or "[]")
    except (ValueError, TypeError):
        return []
    return out if isinstance(out, list) else []


def _row(r, arts, job, run=0, eager=True) -> str:
    settled = r["status"] in store.SETTLED
    paid = bool(r["salary_confirmed"])
    meta = " \u00b7 ".join(x for x in [r["company"], _cap_location(r["location"] or "")] if x)
    status_pill = (f'<span class="status {_h.escape(r["status"], quote=True)}">'
                   f'{_h.escape(r["status"])}</span>' if r["status"] != "new" else "")

    # Contract and interim work said on the title line.
    #
    # It is the first thing that decides whether a role is worth reading at
    # all: the money, the notice period and the reason to take it are all
    # different. A chip filter alone would only say so while the filter was
    # switched on, and the row would look identical to a permanent one every
    # other time you scrolled past it. Nothing is shown for permanent or
    # unstated, because a caption on the other 99% is not information and the
    # facet above counts them.
    emp_pill = ('<span class="emp">contract or interim</span>'
                if _emp(r) == employment.CONTRACT else "")

    # Said on the title line, not only in the documents row underneath.
    # Clicking CV on a role whose CV was already written started a second
    # eight minute run and charged for it, and the reason it was easy to do is
    # that "there is one already" was one line further down and looked like
    # part of the metadata. A ready document is a state of the role, so it
    # belongs where the other states are.
    ready = "".join(
        f'<span class="ready" title="Already drafted. Open it from the buttons '
        f'below.">{name} ready</span>'
        for kind, name in (("screen", "screening"), ("cv", "CV"),
                           ("cover_letter", "letter"))
        if kind in arts)

    docs = []
    if "cv" in arts:
        a = arts["cv"]
        rating = (f'<span class="rating">{a["rating"]:.0f}/100</span>'
                  if a["rating"] else "")
        gates_j = json.loads(a["gates"] or "{}")
        fails = [k for k, v in gates_j.items() if v is False]
        # Name them. "1 gate(s) failed" is a number the reader cannot act on:
        # it does not say which rule, so the only way to find out was to open
        # the rating file. The failing gate is usually the reason to look at
        # the document at all.
        _GATE_NAMES = {
            "written": "nothing was written",
            "no_em_dash": "contains an em-dash",
            "unsourced_specifics": "figures not in your CV",
            "natural_writing": "reads as AI-written",
            "no_overlap_with_cv": "repeats the CV",
        }
        if fails:
            label = ", ".join(_GATE_NAMES.get(k, k.replace("_", " ")) for k in fails)
            found = gates_j.get("unsourced_found") or []
            tip = ("not in your CV: " + ", ".join(map(str, found))) if found else label
            gate = (f'<span class="gatefail" '
                    f'title="{_h.escape(tip, quote=True)}">{_h.escape(label)}</span>')
        else:
            gate = '<span class="gateok">all gates passed</span>' 
        docs.append(f'<a href="/open?path={_h.escape(quote(str(a["path"])), quote=True)}">CV</a> {rating} {gate}')
    if "cover_letter" in arts:
        a = arts["cover_letter"]
        ov = json.loads(a["gates"] or "{}").get("no_overlap_with_cv")
        # None means the check never ran, which is not the same as passing.
        warn = ('' if ov is True else
                '<span class="gatefail">'
                + ('overlaps the CV' if ov is False else 'overlap not checked')
                + '</span>')
        docs.append(f'<a href="/open?path={_h.escape(quote(str(a["path"])), quote=True)}">Cover letter</a> {warn}')
    # The screening is the thing you asked for, so it goes in the row rather
    # than behind a link to a file. <details> gives the minimise for free and
    # keeps working with JavaScript off.
    #
    # Closed by default. It was open, on the reasoning that you clicked Screen
    # to read it. That holds for the first one and stops holding immediately
    # after: screen nine roles and the board is nine walls of analysis you
    # have already read, with the rows you were comparing pushed pages apart.
    # The summary line carries the verdict, which is the part you re-read.
    # The fit score, where it can be read next to the role rather than only
    # in a sort order.
    fitline = ""
    fv = r["fit"] if r["fit"] is not None else -1
    if fv < 0 and len((r["description"] or "").strip()) < 200:
        # An absent score read as "not ranked yet" on a role that can never be
        # ranked. Say which it is, once, quietly.
        fitline = ('<div class="fit none"><b>&mdash;</b>'
                   '<span class="why">no description from this source, so '
                   'there is nothing to score against your CV</span></div>')
    elif fv >= 0:
        band = "good" if fv >= 70 else ("mid" if fv >= 50 else "low")
        fitline = (f'<div class="fit {band}"><b>{fv}</b>'
                   f'<span class="lbl">fit</span>'
                   + (f'<span class="why">{_h.escape(r["fit_why"] or "")}</span>'
                      if r["fit_why"] else "") + '</div>')

    screening = ""
    if "screen" in arts:
        v = arts["screen"]["summary"] or "screened"
        body = (arts["screen"].get("body") or "").strip()
        if body:
            verdict_class = ("skip" if v.upper().startswith("SKIP")
                             else "apply" if v.upper().startswith("APPLY") else "")
            screening = (
                f'<details class="screening"><summary>'
                f'<span class="v {verdict_class}">{_h.escape(v.replace("_", " "))}</span>'
                f'<span class="lbl">screening</span></summary>'
                f'<div class="md">{_md(body)}</div></details>')
        else:
            docs.append(
                f'<a href="/open?path={_h.escape(quote(str(arts["screen"]["path"])), quote=True)}">'
                f'Screening</a> <span class="rating">{_h.escape(v)}</span>')

    # The static page warns when a source gives no description; the served one
    # did not, and that is the page with the money buttons on it.
    # Everything worth a caveat, not just the missing-description one. The
    # note that a salary was never compared to the floor existed on 12 roles
    # in one run and reached no view a person ever opens, so a EUR floor
    # looked like it had passed a set of sterling figures below it. Same for
    # a posting that rules out sponsorship.
    notes = [f for f in _flags(r)
             if ("not screened" in f or "listing only" in f
                 or "not compared" in f or "sponsor" in f)]
    busy = job["kind"] if job else ""
    has_cv = "cv" in arts

    def b(kind, label, cls="", made=""):
        """The button for one kind, which is Open once the thing exists.

        It used to be Draft either way, so clicking CV on a role whose CV was
        already written started a second eight minute agent run and charged
        for it. The document was right there in the row, one line below, as a
        link. A control that spends money has to be the one that says it will:
        the default action on something already made is to look at it.
        """
        on = busy == kind
        if on:
            return f'<button class="{cls} busy" data-gen="{kind}" disabled>{label}</button>'
        art = arts.get(kind)
        if art and art["path"]:
            href = _h.escape(quote(str(art["path"])), quote=True)
            return (f'<a class="btn {cls}" href="/download?path={href}" '
                    f'download title="Save it to your Downloads folder, where '
                    f'the browser\'s upload dialog opens">Download {made or label}</a>'
                    f'<a class="btn" href="/open?path={href}" '
                    f'title="Show it in Finder">Open</a>'
                    # Only where redoing it could give a different answer. A
                    # screening is a reading of an advert that has not
                    # changed, so a second one costs money to reach the same
                    # conclusion; a CV or a letter is a draft, and a draft is
                    # a thing you might want another go at.
                    + (f'<button data-gen="{kind}" data-redraft="1" '
                       f'title="Draft it again. This spends tokens.">Redraft</button>'
                       if kind in ("cv", "cover_letter") else ""))
        return f'<button class="{cls}" data-gen="{kind}">{label}</button>'

    letter_btn = (b("cover_letter", "Cover letter", made="letter") if has_cv else
                  '<button disabled title="Draft the CV first: the letter is '
                  'checked against it for repeated phrasing">Cover letter</button>')

    # The action buttons are the page. Nine controls a row over 4,191 rows is
    # ~60,000 nodes and most of a 7.2MB document, all of it built before the
    # browser will paint, for rows nobody has scrolled to. So they are written
    # for the rows that open on screen and built by `fillActs` for the rest,
    # on hover or focus, from the data attributes below.
    # The action buttons are the page's weight. Nine controls a row across
    # 4,191 rows is most of a 7.2MB document and ~60,000 nodes, every one of
    # them built before the browser will paint, for rows nobody has scrolled
    # to. So they are written for the rows that open on screen and built by
    # `fillActs` for the rest, on hover or focus, out of the data attributes
    # the row already carries.
    acts = (
        '<div class="acts">'
        + b("screen", "Screen", "primary", made="screening")
        + b("cv", "CV", made="CV")
        + letter_btn
        + f'<a class="btn" href="{_h.escape(safe_url(r["url"]))}" target="_blank" '
          f'rel="noopener" data-apply="1">Apply</a>'
        + '<button data-status="skipped">Skip</button>'
        + ('<button data-status="interested">Unskip</button>' if settled else '')
        # The dashboard offered two of the ten statuses and no note, while the
        # CLI had all ten and a note, so the browser could not record an
        # interview date, the thing a tracker is for.
        #
        # The options are not here either: ten a row was 43,600 <option> nodes
        # and 2.3MB on its own. `fillStatus` adds them on first interaction,
        # and the current status rides on the element so `mark()` can read it
        # without them existing.
        + f'<select class="setstatus" aria-label="Set status" '
          f'data-current="{_h.escape(r["status"] or "")}">'
        + f'<option value="">{_h.escape(r["status"] or "Status")}\u2026</option>'
        + '</select>'
        + '<button data-note="1" title="Add or edit a note">Note</button>'
        + '</div>'
    )

    return (
        f'<div class="row{" settled" if settled else ""}" data-uid="{_h.escape(r["uid"], quote=True)}" '
        f'data-status="{_h.escape(r["status"], quote=True)}" '
        f'data-pay="{1 if paid else 0}" '
        f'data-new="{1 if r["uid"] in run else 0}" '
        # The bottom of a stated range, and only that. This fell back to the
        # top when a posting stated no bottom, so "up to 175,000" reported a
        # floor of 175,000 and led the salary sort ahead of a role actually
        # guaranteeing 150,000. The ceiling is kept beside it under its own
        # name, and the currency travels with them because comparing figures
        # across currencies is a guess this tool refuses to make elsewhere.
        f'data-payfloor="{int(r["salary_min"] or 0)}" '
        f'data-paytop="{int(r["salary_max"] or r["salary_min"] or 0)}" '
        f'data-paycur="{_h.escape(r["salary_currency"] or "", quote=True) if paid else ""}" '
        f'data-seen="{_h.escape(str(r["first_seen"] or ""), quote=True)}" '
        f'data-fit="{r["fit"] if r["fit"] is not None else -1}" '
        f'data-score="{r["score"] or 0}" '
        f'data-sector="{_h.escape(r["sector"] or "other", quote=True)}" '
        f'data-mode="{_h.escape(r["work_mode"] or "unstated", quote=True)}" '
        f'data-emp="{_h.escape(_emp(r), quote=True)}" '
        f'data-country="{_h.escape(r["country"] or "unknown", quote=True)}" '
        f'data-city="{_h.escape(r["city"] or "", quote=True)}" '
        # Read by `fillActs` to build this row's buttons when it is first
        # hovered or focused. Cheap attributes, against ~14 elements each.
        f'data-url="{_h.escape(safe_url(r["url"]), quote=True)}" '
        f'data-hascv="{1 if has_cv else 0}" '
        # The address of anything already generated for this role, so a lazy
        # row can offer Open rather than a button that would pay to make a
        # second one. Read off `arts` rather than off the rendered docs line:
        # the screening is shown inline in a <details> and has no link there
        # at all, so a reader of the markup would have decided no screening
        # existed and offered to run it again.
        + "".join(
            f'data-open-{suffix}="/open?path='
            f'{_h.escape(quote(str(arts[k]["path"])), quote=True)}" '
            for k, suffix in DOC_ATTR.items()
            if arts.get(k) and arts[k]["path"])
        + f'data-settled="{1 if settled else 0}"'
        # The attribute is built outside the f-string on purpose. A backslash
        # inside an f-string expression is a SyntaxError before Python 3.12,
        # and this file parsed on 3.13 here while failing to import on 3.10
        # and 3.11 in CI: thirteen red runs whose only symptom was 26 test
        # files "could not import".
        + _LAZY_ATTR[bool(eager)]
        + '>'
        f'<div class="pick"><input type="checkbox" class="sel" '
        f'aria-label="Select for bulk screening"></div>'
        f'<div><div class="role">'
        f'<a href="{_h.escape(safe_url(r["url"]))}" target="_blank" rel="noopener">{_h.escape(r["title"])}</a>'
        f'{emp_pill}{status_pill}{ready}</div>'
        f'<div class="meta">{_h.escape(meta)}</div></div>'
        f'<div class="right"><span class="pay{"" if paid else " unk"}">'
        f'{_h.escape(r["salary_label"] or "unconfirmed salary")}</span></div>'
        + (f'<div class="docs">{" &middot; ".join(docs)}</div>' if docs else "")
        # All of them, not notes[0]. A role could be both unscreenable and
        # carrying a salary that was never compared to the floor, and only
        # the first ever appeared.
        + "".join(f'<div class="note">{_h.escape(n)}</div>' for n in notes)
        + fitline
        + (f'<div class="rownote">{_h.escape(r["note"])}</div>' if r["note"] else "")
        + screening
        + (acts if eager else "")
        + (f'<div class="err" hidden></div>')
        + '</div>')
