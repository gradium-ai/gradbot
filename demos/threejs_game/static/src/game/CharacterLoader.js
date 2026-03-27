import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';

/**
 * Loads a GLB character model and prepares it for use.
 * Returns { model, mixer, animations, skeleton } so the caller
 * can wire up animation state however it likes.
 */
export class CharacterLoader {
  constructor() {
    this._loader = new GLTFLoader();
  }

  /**
   * @param {string} url  Path to the .glb file
   * @param {object} opts
   * @param {number} [opts.scale=1]       Uniform scale
   * @param {boolean} [opts.castShadow=true]
   * @param {boolean} [opts.receiveShadow=false]
   * @returns {Promise<{model: THREE.Group, mixer: THREE.AnimationMixer, animations: Map<string,THREE.AnimationClip>, skeleton: THREE.Skeleton|null}>}
   */
  load(url, opts = {}) {
    const { scale = 1, castShadow = true, receiveShadow = false } = opts;

    return new Promise((resolve, reject) => {
      this._loader.load(
        url,
        (gltf) => {
          const model = gltf.scene;

          // Normalize scale
          model.scale.setScalar(scale);

          // Enable shadows on all skinned meshes
          let skeleton = null;
          model.traverse((child) => {
            if (child.isMesh) {
              child.castShadow = castShadow;
              child.receiveShadow = receiveShadow;
              child.frustumCulled = false;
            }
            if (child.isSkinnedMesh && !skeleton) {
              skeleton = child.skeleton;
            }
          });

          // Build animation mixer + name-indexed map
          const mixer = new THREE.AnimationMixer(model);
          const animations = new Map();
          for (const clip of gltf.animations) {
            animations.set(clip.name, clip);
          }

          resolve({ model, mixer, animations, skeleton });
        },
        undefined,
        reject
      );
    });
  }
}
