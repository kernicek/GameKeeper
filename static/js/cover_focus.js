// Draggable focal-point marker for the cover editor (issues #12, #13).
//
// The marker <span> in the cover picker can be dragged to set the crop focal
// point, instead of only clicking the art. Handlers are DELEGATED on document
// so they survive htmx swapping the whole #cover-column on every save — there
// is nothing to re-initialise after a swap.
//
// On release we POST x/y to the same endpoint the art click uses, read off the
// frame's <img hx-post="...">. Holding Shift while dragging locks movement to
// the axis moved furthest from the grab point (move on one axis only).
(function () {
    "use strict";

    var drag = null;

    function pct(frame, clientX, clientY) {
        var r = frame.getBoundingClientRect();
        return {
            x: clamp(Math.round((clientX - r.left) * 100 / r.width)),
            y: clamp(Math.round((clientY - r.top) * 100 / r.height))
        };
    }

    function clamp(v) {
        return Math.max(0, Math.min(100, v));
    }

    document.addEventListener("pointerdown", function (e) {
        var marker = e.target.closest && e.target.closest(".cover-focus-marker");
        if (!marker) {
            return;
        }
        var frame = marker.closest(".cover-focus-frame");
        var img = frame && frame.querySelector("img[hx-post]");
        if (!img) {
            return;
        }
        e.preventDefault();
        marker.setPointerCapture(e.pointerId);
        var start = pct(frame, e.clientX, e.clientY);
        drag = {
            marker: marker, frame: frame, img: img, id: e.pointerId,
            startX: start.x, startY: start.y, last: null
        };
    });

    document.addEventListener("pointermove", function (e) {
        if (!drag || e.pointerId !== drag.id) {
            return;
        }
        var p = pct(drag.frame, e.clientX, e.clientY);
        if (e.shiftKey) {
            // Lock to the axis dragged furthest from where the grab started.
            if (Math.abs(p.x - drag.startX) >= Math.abs(p.y - drag.startY)) {
                p.y = drag.startY;
            } else {
                p.x = drag.startX;
            }
        }
        drag.marker.style.left = p.x + "%";
        drag.marker.style.top = p.y + "%";
        drag.last = p;
    });

    document.addEventListener("pointerup", function (e) {
        if (!drag || e.pointerId !== drag.id) {
            return;
        }
        var img = drag.img;
        var last = drag.last;
        drag = null;
        // A click without a drag has no new position — nothing to save.
        if (last) {
            htmx.ajax("POST", img.getAttribute("hx-post"), {
                target: "#cover-column",
                values: { x: last.x, y: last.y }
            });
        }
    });
})();
