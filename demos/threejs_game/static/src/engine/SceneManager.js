import * as THREE from 'three';
import { CONFIG } from '../config.js';

export class SceneManager {
  constructor() {
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color(0x1a1a1a);

    // Perspective camera for first-person view
    this.camera = new THREE.PerspectiveCamera(
      CONFIG.CAM_FOV,
      window.innerWidth / window.innerHeight,
      CONFIG.CAM_NEAR,
      CONFIG.CAM_FAR
    );
    this.camera.position.set(0, CONFIG.PLAYER_EYE_HEIGHT, 4);

    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setSize(window.innerWidth, window.innerHeight);
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.toneMapping = THREE.LinearToneMapping;
    this.renderer.toneMappingExposure = 1.5;
    document.body.prepend(this.renderer.domElement);

    this._setupLights();

    window.addEventListener('resize', () => this._onResize());
  }

  _setupLights() {
    // Bright ambient for clinical Lumon feel
    this.ambientLight = new THREE.AmbientLight(0xffffff, 2.0);
    this.scene.add(this.ambientLight);

    // Soft hemisphere light (ceiling white, floor green bounce)
    this.hemiLight = new THREE.HemisphereLight(0xffffff, 0x5a9a5a, 0.8);
    this.scene.add(this.hemiLight);

    // Main directional (overhead fluorescent feel)
    this.dirLight = new THREE.DirectionalLight(0xfff8f0, 1.0);
    this.dirLight.position.set(2, 10, 2);
    this.dirLight.castShadow = true;
    this.dirLight.shadow.mapSize.set(2048, 2048);
    this.dirLight.shadow.camera.near = 0.5;
    this.dirLight.shadow.camera.far = 20;
    this.dirLight.shadow.camera.left = -12;
    this.dirLight.shadow.camera.right = 12;
    this.dirLight.shadow.camera.top = 10;
    this.dirLight.shadow.camera.bottom = -10;
    this.scene.add(this.dirLight);
  }

  _onResize() {
    this.camera.aspect = window.innerWidth / window.innerHeight;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(window.innerWidth, window.innerHeight);
  }

  render() {
    this.renderer.render(this.scene, this.camera);
  }
}
