// Server-clock offset.  Several live displays (karaoke lyrics, countdowns, the
// stopwatch, the Closest meter) compare a server-stamped start time against the
// browser's own clock.  The Pi is offline at the campfire and can boot with a stale
// clock, and the projector/iPad each keep their own time -- so those two can
// disagree by seconds, hours or days.  srvNow() is "the server's now" as best we can
// estimate it (Cristian's algorithm: offset = server_now - local midpoint of the
// request), which makes the elapsed-time maths independent of any clock agreement.
var _srvOffsetMs = 0;
function srvNow() { return Date.now() + _srvOffsetMs; }
function syncServerClock() {
  var t0 = Date.now();
  return fetch('/api/time', {cache: 'no-store'}).then(function (r) { return r.json(); }).then(function (d) {
    var t1 = Date.now();
    if (d && d.now_ms) _srvOffsetMs = d.now_ms - (t0 + t1) / 2;
  }).catch(function () {});
}
syncServerClock();
setInterval(syncServerClock, 300000);
