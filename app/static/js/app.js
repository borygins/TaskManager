document.addEventListener("DOMContentLoaded", () => {
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
