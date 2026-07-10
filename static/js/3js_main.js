// Initialize Vanta.js 3D Neural Network Background
document.addEventListener('DOMContentLoaded', () => {
    // Detect if user is on a mobile device (screen width <= 768px)
    const isMobile = window.innerWidth <= 768;

    try {
        VANTA.NET({
            el: "#vanta-bg",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 200.00,
            minWidth: 200.00,
            scale: 1.00,
            scaleMobile: 0.50, // Lower rendering resolution on mobile for high FPS
            color: 0xa855f7, // matching primary brand color
            backgroundColor: 0x0a0816, // matching bg-base
            // Reduce dots and connections on mobile to prevent lag
            points: isMobile ? 6.00 : 12.00,
            maxDistance: isMobile ? 16.00 : 22.00,
            spacing: isMobile ? 24.00 : 16.00,
            showDots: true
        });
    } catch(e) {
        console.log("Vanta.js failed to load.");
    }
    
    // Initialize 3D Glass Tilt Effect on Cards (Desktop Only)
    // Disabled on mobile because touch devices don't have hover state and it consumes GPU
    if (!isMobile) {
        try {
            if (typeof VanillaTilt !== 'undefined') {
                VanillaTilt.init(document.querySelectorAll(".analyzer-input-card, .video-card, .transcript-card"), {
                    max: 2, // max tilt rotation
                    speed: 400, // speed of the enter/exit transition
                    glare: true,
                    "max-glare": 0.15,
                });
            }
        } catch(e) {
            console.log("VanillaTilt failed to load.");
        }
    }
});
