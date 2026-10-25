document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-export-confirm]").forEach((link) => {
        let pending = false;
        link.addEventListener("click", async (event) => {
            event.preventDefault();
            if (pending) return;
            pending = true;
            link.setAttribute("aria-busy", "true");
            try {
                const previewUrl = new URL(link.href, window.location.href);
                previewUrl.searchParams.set("preview", "true");
                const response = await fetch(previewUrl, {
                    headers: { "Accept": "application/json" }, cache: "no-store",
                });
                if (!response.ok || response.redirected) throw new Error("Export preview failed");
                const { count } = await response.json();
                if (!Number.isInteger(count) || count < 0) throw new Error("Invalid export count");
                if (window.confirm(`Выгрузить ${count} записей клиентов?`)) {
                    const downloadUrl = new URL(link.href, window.location.href);
                    downloadUrl.searchParams.set("confirmed", "true");
                    window.location.assign(downloadUrl.href);
                }
            } catch {
                window.alert("Не удалось получить количество записей. Обновите страницу и попробуйте снова.");
            } finally {
                pending = false;
                link.removeAttribute("aria-busy");
            }
        });
    });
    document.querySelectorAll("[data-contact-form]").forEach((form) => {
        const kind = form.elements.activity_type;
        const missedCall = form.elements.mark_missed_call;
        const nextDate = form.elements.next_activity_date;
        const update = () => {
            const meeting = kind.value === "Встреча";
            missedCall.disabled = meeting;
            if (meeting) missedCall.checked = false;
            nextDate.required = missedCall.checked;
            nextDate.min = form.elements.activity_date.value;
        };
        [kind, missedCall, form.elements.activity_date].forEach((input) => input.addEventListener("change", update));
        form.addEventListener("submit", () => {
            const button = form.querySelector('button[type="submit"]');
            button.disabled = true;
            button.textContent = "Сохраняем…";
        });
        window.addEventListener("pageshow", () => {
            const button = form.querySelector('button[type="submit"]');
            button.disabled = false;
            button.textContent = "Сохранить";
        });
        update();
    });
    const cards = document.querySelectorAll(".client-card, .panel, .funnel-card, .stat-card");
    cards.forEach((card, index) => {
        card.animate(
            [
                { opacity: 0, transform: "translateY(8px)" },
                { opacity: 1, transform: "translateY(0)" }
            ],
            {
                duration: 260 + index * 20,
                fill: "both",
                easing: "ease-out"
            }
        );
    });
});
