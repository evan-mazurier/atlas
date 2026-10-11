/* Team creatures: shapes, gradients, motion phase. Copied from cockpit/web/team.html (docs/v2/contracts.md §4, §8.4).
   The operator's "kept exactly" wins: the code between the markers is verbatim, hex literals included. */
/* creatures: verbatim from v1 */
const T = {
  op:{name:"Operator",tier:"human"}, chief:{name:"Chief",tier:"opus"}, scout:{name:"Scout",tier:"haiku"}, clerk:{name:"Clerk",tier:"haiku"},
  analyst:{name:"Analyst",tier:"sonnet"}, builder:{name:"Builder",tier:"sonnet"}, writer:{name:"Writer",tier:"sonnet"},
  labtech:{name:"Lab Tech",tier:"haiku"}, inspector:{name:"Inspector",tier:"sonnet"}, partner:{name:"Senior Partner",tier:"opus"}
};
const G = {haiku:["#33E3C2","#2F7BFF"], sonnet:["#3D86FF","#6B4DFF"], opus:["#8B4DFF","#E040FB"]};
const SHAPE = {
  chief:{d:"M12 36H88C88 61 71 79 50 79S12 61 12 36Z",e:[50,55,13],h:"bar",px:[[36,24,5],[46,16,6],[58,26,4],[66,19,5]]},
  scout:{d:"M50 12C59 30 75 43 75 60A25 25 0 0 1 25 60C25 43 41 30 50 12Z",e:[50,62,11],h:"dot",px:[[64,10,4],[70,18,3]]},
  clerk:{d:"M20 76C8 76 8 58 19 55C19 40 36 36 44 45C50 34 69 34 73 47C87 45 93 61 85 72C83 76 79 76 75 76Z",e:[39,61,10],h:"bar",px:[[26,26,4],[34,18,5],[42,28,3],[50,22,4]]},
  analyst:{d:"M28 82C28 52 33 26 54 22C71 19 78 34 71 47C85 55 91 71 89 82Z",e:[55,38,9],h:"slant",px:[[50,8,4],[58,12,5],[64,4,3]]},
  builder:{d:"M22 30H78L71 70C69 79 60 84 50 84S31 79 29 70Z",e:[50,66,10],h:"flat",px:[[40,18,4],[48,12,6],[60,20,4]]},
  writer:{d:"M63 15C81 27 83 56 67 74C58 83 40 85 30 77C21 70 24 58 34 54C46 50 52 40 52 30C52 21 56 15 63 15Z",e:[45,64,9],h:"dot",px:[[70,6,3],[76,12,4]]},
  labtech:{d:"M50 20A26 25 0 1 1 49.9 20ZM38 66H62V80H38Z",e:[50,45,12],h:"dot",px:[[30,14,4],[38,8,3],[66,12,5]]},
  inspector:{d:"M50 14L81 25V50C81 67 69 79 50 87C31 79 19 67 19 50V25Z",e:[50,49,12],h:"bar",px:[[44,4,4],[54,6,3]]},
  partner:{d:"M25 85V46C25 29 36 17 50 17S75 29 75 46V85Z",e:[50,46,12],h:"slant",px:[[42,6,4],[50,2,5],[58,7,3]]}
};
const ORDER = ["op","chief","scout","clerk","analyst","builder","writer","labtech","inspector","partner"];

// Creature art. The random-looking motion phases come from a hash of a stable key (not a counter), so a
// creature that is redrawn keeps its own rhythm instead of jumping to a new one every poll.
let uid = 0;
function hash(s){ let h = 2166136261; for (let i = 0; i < s.length; i++){ h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }
function creature(id, key){
  const s = SHAPE[id], g = G[T[id].tier], k = "g" + (uid++), [ex,ey,er] = s.e;
  const hl = {bar:`<rect x="${ex-2}" y="${ey-er*.6}" width="4" height="${er*1.2}" rx="2" fill="#fff"/>`,
    dot:`<circle cx="${ex+er*.35}" cy="${ey-er*.1}" r="${er*.32}" fill="#fff"/>`,
    slant:`<rect x="${ex-1.8}" y="${ey-er*.6}" width="3.6" height="${er*1.2}" rx="1.8" fill="#fff" transform="rotate(-45 ${ex} ${ey})"/>`,
    flat:`<rect x="${ex-er*.6}" y="${ey-1.6}" width="${er*1.2}" height="3.2" rx="1.6" fill="#fff"/>`}[s.h];
  const r = n => hash(key + ":" + n);
  const px = s.px.map(([x,y,z],i) => `<rect class="px" style="animation-delay:${(i*.45)%1.6}s" x="${x}" y="${y}" width="${z}" height="${z}" fill="${g[i%2]}"/>`).join("");
  return `<svg viewBox="0 0 100 100" style="--d:${(1.2+r(1)*.5).toFixed(2)}s;--dl:${(-r(2)*2).toFixed(2)}s;--b:${(4+r(3)*3).toFixed(2)}s;--bl:${(-r(4)*5).toFixed(2)}s;overflow:visible" aria-hidden="true">
    <defs><linearGradient id="${k}" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="${g[0]}"/><stop offset="1" stop-color="${g[1]}"/></linearGradient>
    <filter id="${k}f" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="3"/></filter></defs>
    <ellipse cx="50" cy="94" rx="26" ry="4" fill="${g[0]}" opacity=".45" filter="url(#${k}f)"/>
    <g class="hop">${px}<path d="${s.d}" fill="url(#${k})"/><g class="lid"><circle cx="${ex}" cy="${ey}" r="${er}" fill="#05060A"/><g class="look">${hl}</g></g></g></svg>`;
}
function human(){
  return `<svg viewBox="0 0 100 100" aria-hidden="true"><path d="M18 92C19 74 32 64 50 64S81 74 82 92Z" fill="#2a2f40"/><path d="M43 64L50 73L57 64" fill="none" stroke="#9b5cff" stroke-width="2"/><rect x="44" y="52" width="12" height="13" rx="4" fill="#D9A47E"/><ellipse cx="50" cy="39" rx="17" ry="19" fill="#E8B996"/><path d="M33 37C32 23 41 17 51 17C61 17 69 23 67 36C64 29 58 26 50 26C42 26 37 29 33 37Z" fill="#3A2A1E"/><circle cx="43.5" cy="41" r="2" fill="#1B1F23"/><circle cx="56.5" cy="41" r="2" fill="#1B1F23"/><path d="M44 49C47.5 52 52.5 52 56 49" fill="none" stroke="#1B1F23" stroke-width="2" stroke-linecap="round"/></svg>`;
}
const art = (id, key) => id === "op" ? human() : (SHAPE[id] ? creature(id, key) : "");
/* end creatures */

export { T, G, SHAPE, ORDER, art, creature, human, hash };
