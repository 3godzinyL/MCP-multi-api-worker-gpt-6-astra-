import * as THREE from './vendor/three.module.min.js';
import {FontLoader} from './vendor/FontLoader.js';
import {TextGeometry} from './vendor/TextGeometry.js';
import {RoomEnvironment} from './vendor/RoomEnvironment.js';
import {t, onLanguageChange} from './i18n.js';

export async function mountPolcio(container, toggle) {
  const fontData=await fetch('/ui/assets/vendor/helvetiker_bold.typeface.json').then(r=>{if(!r.ok)throw new Error('font');return r.json()});
  const font=new FontLoader().parse(fontData);
  const renderer=new THREE.WebGLRenderer({antialias:true,alpha:true,powerPreference:'low-power'});
  renderer.setPixelRatio(Math.min(devicePixelRatio,1.75));
  renderer.toneMapping=THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure=1.12;
  renderer.shadowMap.enabled=true;
  renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  container.replaceChildren(renderer.domElement);
  renderer.domElement.setAttribute('aria-hidden','true');
  const scene=new THREE.Scene();
  const pmrem=new THREE.PMREMGenerator(renderer);
  const room=new RoomEnvironment();
  const environment=pmrem.fromScene(room,.04);
  scene.environment=environment.texture;
  room.dispose();pmrem.dispose();
  const camera=new THREE.PerspectiveCamera(34,1,.1,80);
  scene.add(new THREE.HemisphereLight(0xffe4a1,0x59623d,2));
  const key=new THREE.DirectionalLight(0xffe8ae,5);
  key.position.set(-3,6,5);key.castShadow=true;key.shadow.mapSize.set(1024,1024);
  key.shadow.camera.left=-9;key.shadow.camera.right=9;key.shadow.camera.top=6;key.shadow.camera.bottom=-6;key.shadow.bias=-.001;
  scene.add(key);
  const rim=new THREE.PointLight(0xc5ef84,25,18);rim.position.set(4,.7,-2);scene.add(rim);
  const fill=new THREE.PointLight(0xfff3ce,18,20);fill.position.set(-5,1.2,3);scene.add(fill);
  const gold=new THREE.MeshPhysicalMaterial({color:0xeec65d,metalness:.92,roughness:.22,clearcoat:1,clearcoatRoughness:.12,envMapIntensity:2});
  const goo=new THREE.MeshPhysicalMaterial({color:0xc2bd3f,metalness:.38,roughness:.14,clearcoat:1,clearcoatRoughness:.03,transmission:.12,thickness:.5,ior:1.44,envMapIntensity:2.2});
  const group=new THREE.Group();scene.add(group);
  const letters=[],drips=[];
  const sphere=new THREE.SphereGeometry(1,20,14);
  let offset=0;
  function buildLetters(){
  for(const letter of letters){group.remove(letter);letter.geometry.dispose()}
  for(const drip of drips){scene.remove(drip.stem,drip.drop,drip.puddle);drip.stem.geometry.dispose()}
  letters.length=0;drips.length=0;offset=0;
  for(const character of t('Polcio to szef')){
    if(character===' '){offset+=.38;continue}
    const geometry=new TextGeometry(character,{font,size:.85,depth:.21,curveSegments:10,bevelEnabled:true,bevelSegments:4,bevelSize:.018,bevelThickness:.023});
    geometry.computeBoundingBox();
    const box=geometry.boundingBox,width=box.max.x-box.min.x;
    geometry.translate(-(box.min.x+box.max.x)/2,-(box.min.y+box.max.y)/2,-.105);
    const letter=new THREE.Mesh(geometry,gold);letter.castShadow=true;letter.receiveShadow=true;
    letter.position.set(offset+width/2,.55,0);group.add(letter);letters.push(letter);offset+=width+.12;
  }
  for(const letter of letters)letter.position.x-=offset/2;
  for(let i=0;i<letters.length;i++){
    const stemGeometry=new THREE.CylinderGeometry(1,1,1,12,18);
    const original=Float32Array.from(stemGeometry.attributes.position.array);
    const stem=new THREE.Mesh(stemGeometry,goo);stem.castShadow=true;
    const drop=new THREE.Mesh(sphere,goo);drop.castShadow=true;
    const puddle=new THREE.Mesh(sphere,goo);puddle.position.set(letters[i].position.x,-1.44,.02);puddle.scale.set(.18,.035,.11);puddle.receiveShadow=true;
    scene.add(stem,drop,puddle);
    drips.push({stem,drop,puddle,original,phase:i*.37,anchor:letters[i].position.clone()});
  }
  }
  buildLetters();
  const floor=new THREE.Mesh(new THREE.PlaneGeometry(40,20),new THREE.MeshStandardMaterial({color:0x202218,metalness:.42,roughness:.45}));
  floor.rotation.x=-Math.PI/2;floor.position.y=-1.49;floor.receiveShadow=true;scene.add(floor);
  let visible=true,intersecting=true,moving=!matchMedia('(prefers-reduced-motion: reduce)').matches,frame=0,previous=0,time=0;
  function resize(){const width=Math.max(1,container.clientWidth),height=Math.max(1,container.clientHeight);renderer.setSize(width,height,false);camera.aspect=width/height;camera.position.set(0,1.1,Math.max(5.5,offset/(2*Math.tan(THREE.MathUtils.degToRad(17))*camera.aspect)*1.18));camera.lookAt(0,-.22,0);camera.updateProjectionMatrix();draw()}
  function draw(){
    for(let i=0;i<letters.length;i++){
      const letter=letters[i];letter.rotation.y=time*.23+Math.sin(time*.35+i*.2)*.1;
      letter.rotation.z=Math.sin(time*.53+i*.43)*.045;letter.position.y=.55+Math.sin(time*.7+i*.5)*.045;
    }
    for(let i=0;i<drips.length;i++){
      const d=drips[i],cycle=(time*.34+d.phase)%2.6,attached=cycle<1.6;
      const x=d.anchor.x+Math.sin(time*.6+i)*.025,top=letters[i].position.y-.34;
      const length=attached?.09+Math.pow(cycle/1.6,1.65)*1.65:Math.max(.06,(2.6-cycle)*.24);
      const radius=attached?.07+.025*cycle:.10;
      const positions=d.stem.geometry.attributes.position;
      for(let j=0;j<positions.count;j++){
        const t=d.original[j*3+1]+.5;
        const r=(.021+.035*Math.pow(Math.abs(t-.5)*2,3))*(attached?1:Math.max(.2,2.6-cycle));
        positions.setXYZ(j,d.original[j*3]*r+Math.sin(t*5+time+i)*.007,(t-.5)*length,d.original[j*3+2]*r);
      }
      positions.needsUpdate=true;d.stem.geometry.computeVertexNormals();d.stem.position.set(x,top-length/2,.025);
      d.drop.position.set(x,attached?top-length:top-1.74-Math.pow(cycle-1.6,2)*4,.025);
      d.drop.visible=d.drop.position.y>-1.40;
      d.drop.scale.set(radius,radius*(attached?1.3+cycle*.3:1.6),radius*.88);
      const pulse=Math.max(0,Math.sin((cycle-1.75)*4));
      d.puddle.scale.set(.17+pulse*.075,.025+pulse*.006,.12+pulse*.035);
    }
    renderer.render(scene,camera);
  }
  function animate(now){frame=0;if(!moving||!visible||!intersecting||document.hidden){previous=0;return}if(previous)time+=Math.min(.05,(now-previous)/1000);previous=now;draw();frame=requestAnimationFrame(animate)}
  function run(){if(moving&&visible&&intersecting&&!document.hidden&&!frame)frame=requestAnimationFrame(animate);else if(!moving||!visible||!intersecting||document.hidden){cancelAnimationFrame(frame);frame=0;previous=0}toggle.textContent=t(moving?'Wstrzymaj animację':'Włącz animację');toggle.setAttribute('aria-pressed',String(!moving))}
  toggle.addEventListener('click',()=>{moving=!moving;run();draw()});
  const observer=new IntersectionObserver(entries=>{intersecting=entries[0].isIntersecting;run()});observer.observe(container);
  const resizeObserver=new ResizeObserver(resize);resizeObserver.observe(container);
  document.addEventListener('visibilitychange',run);
  onLanguageChange(()=>{buildLetters();resize();run()});
  resize();run();
  return {setVisible(value){visible=value;run();if(value)resize()}};
}
