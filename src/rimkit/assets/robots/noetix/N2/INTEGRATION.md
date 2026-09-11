# N2 RIMKit integration

The N2 model is loaded through RIMKit's shared scene wrapper at
`assets/scenes/n2.xml`.

The RIMKit-local MJCF adds fixed, massless semantic frames without changing
the N2 kinematic or actuator layout:

- `N2_spine_link` lies on the neutral hip-to-neck line, preserving non-zero,
  proportionate base-to-spine and spine-to-neck DMR segments;
- `L_arm_hand_Link` and `R_arm_hand_Link` move the existing hand meshes into
  body frames used as position-only end effectors;
- sole and toe frames use the lower support plane and forward edge of each
  ankle mesh's local bounds.

N2 has no wrist or toe actuators, so its DMR profile disables hand and ankle
orientation tasks while retaining position and contact refinement.
