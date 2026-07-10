//!HOOK OUTPUT
//!BIND HOOKED
//!DESC RTSP-TOOL contrast-adaptive sharpen (après mise à l'échelle)

// Accentuation adaptative inspirée d'AMD FidelityFX CAS : renforce les contours
// proportionnellement au contraste local, sans créer de halos sur les aplats.
// S'applique à la résolution d'AFFICHAGE → effet visible même quand la source
// est réduite dans une petite tuile. Écrit pour RTSP-TOOL (licence MIT du projet).

#define SHARPNESS 0.55

vec4 hook() {
    vec3 e = HOOKED_texOff(vec2( 0.0,  0.0)).rgb;
    vec3 b = HOOKED_texOff(vec2( 0.0, -1.0)).rgb;
    vec3 d = HOOKED_texOff(vec2(-1.0,  0.0)).rgb;
    vec3 f = HOOKED_texOff(vec2( 1.0,  0.0)).rgb;
    vec3 h = HOOKED_texOff(vec2( 0.0,  1.0)).rgb;

    vec3 mn = min(min(min(d, e), min(f, b)), h);
    vec3 mx = max(max(max(d, e), max(f, b)), h);

    // poids d'accentuation : fort là où il reste de la marge de contraste
    vec3 amp = sqrt(clamp(min(mn, 1.0 - mx) / (mx + 1e-5), 0.0, 1.0));
    float peak = -1.0 / mix(8.0, 5.0, SHARPNESS);
    vec3 w = amp * peak;

    vec3 res = ((b + d + f + h) * w + e) / (4.0 * w + 1.0);
    return vec4(clamp(res, 0.0, 1.0), 1.0);
}
