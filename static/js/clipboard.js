// Copy-to-clipboard buttons next to share links (issue #125). Delegated on
// document so it keeps working after htmx swaps #sharing-settings.
(function () {
    "use strict";

    function fallbackCopy(value) {
        var scratch = document.createElement("textarea");
        scratch.value = value;
        scratch.style.position = "fixed";
        scratch.style.opacity = "0";
        document.body.appendChild(scratch);
        scratch.select();
        document.execCommand("copy");
        document.body.removeChild(scratch);
    }

    function showCopied(button) {
        var tooltip = bootstrap.Tooltip.getOrCreateInstance(button, {
            trigger: "manual", title: "Copied!", placement: "top"
        });
        tooltip.show();
        setTimeout(function () { tooltip.hide(); }, 1500);
    }

    document.addEventListener("click", function (e) {
        var button = e.target.closest && e.target.closest(".js-copy-link");
        if (!button) {
            return;
        }
        var value = button.getAttribute("data-copy-value");
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(value).then(function () {
                showCopied(button);
            });
        } else {
            fallbackCopy(value);
            showCopied(button);
        }
    });
})();
