// Initialize Vanta.js 3D Neural Network Background
document.addEventListener('DOMContentLoaded', () => {
    try {
        VANTA.NET({
            el: "#vanta-bg",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 200.00,
            minWidth: 200.00,
            scale: 1.00,
            scaleMobile: 1.00,
            color: 0xa855f7, // matching primary brand color
            backgroundColor: 0x0a0816, // matching bg-base
            points: 12.00,
            maxDistance: 22.00,
            spacing: 16.00,
            showDots: true
        });
    } catch(e) {
        console.log("Vanta.js failed to load.");
    }
    
    // Initialize 3D Glass Tilt Effect on Cards (if VanillaTilt is loaded)
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
});
