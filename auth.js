// ============================================================
// auth.js — Firebase Authentication + Firestore rol yönetimi
// ============================================================
// KURULUM:
// 1) https://console.firebase.google.com adresinden yeni proje oluştur.
// 2) Authentication → Sign-in method → "E-posta/Şifre" sağlayıcısını aç.
// 3) Firestore Database → veritabanı oluştur (production mode).
// 4) Firestore → Rules sekmesine firestore.rules dosyasındaki kuralları yapıştır.
// 5) Proje Ayarları → Genel → "Web uygulaması ekle" ile aldığın config
//    nesnesini aşağıdaki firebaseConfig alanına yapıştır.
// 6) index.html, altin-karsilastirma.html ve giris.html dosyalarını
//    aynı klasördeki js/auth.js'i import edecek şekilde bırak
//    (zaten altin-karsilastirma.html içinde hazır).
// ============================================================

import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import {
  getAuth,
  onAuthStateChanged,
  signInWithEmailAndPassword,
  createUserWithEmailAndPassword,
  signOut,
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import {
  getFirestore,
  doc,
  getDoc,
  setDoc,
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";

// ── TODO: kendi Firebase proje bilgilerinle değiştir ──
const firebaseConfig = {
  apiKey: "AIzaSyBU6sP7Jclmemw4BqWMMdMhCimhA1yWfnU",
  authDomain: "altin-portfoy.firebaseapp.com",
  projectId: "altin-portfoy",
  storageBucket: "altin-portfoy.firebasestorage.app",
  messagingSenderId: "787635696995",
  appId: "1:787635696995:web:bf8b21e9225c6109eecf56",
  measurementId: "G-Y9V0GR02M6"
};


const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);

// Admin olmayan (sıradan üye) kullanıcıların erişebileceği tek sayfa.
// requireAdmin:true olan bir sayfaya admin olmayan biri girmeye çalışırsa
// buraya yönlendirilir.
const MEMBER_ALLOWED_PAGE = "altin-karsilastirma.html";
// Giriş yapmamış kullanıcının yönlendirileceği sayfa.
const LOGIN_PAGE = "giris.html";

// ── Kayıt (Üye Ol) ──────────────────────────────────────────
// Yeni hesaplar HER ZAMAN 'user' rolüyle oluşturulur.
// Admin rolü yalnızca Firebase Console → Firestore → users/{uid} →
// role alanını elle "admin" yaparak verilir (bkz. firestore.rules).
export async function register(email, password) {
  const cred = await createUserWithEmailAndPassword(auth, email, password);
  await setDoc(doc(db, "users", cred.user.uid), {
    email,
    role: "user",
    createdAt: new Date().toISOString(),
  });
  return cred.user;
}

// ── Giriş ───────────────────────────────────────────────────
export function login(email, password) {
  return signInWithEmailAndPassword(auth, email, password);
}

// ── Çıkış ───────────────────────────────────────────────────
export function logout() {
  return signOut(auth).then(() => {
    window.location.href = LOGIN_PAGE;
  });
}

// ── Sayfa koruma ────────────────────────────────────────────
// requireAdmin: true  → sadece admin rolündeki kullanıcılar girebilir
//                        (örn. index.html / kişisel portföy sayfası)
// requireAdmin: false → giriş yapmış her üye girebilir
//                        (örn. altin-karsilastirma.html)
// onReady(user, isAdmin) → yetki kontrolü geçtikten sonra çağrılır
export function guardPage({ requireAdmin = false, onReady }) {
  onAuthStateChanged(auth, async (user) => {
    if (!user) {
      window.location.href = LOGIN_PAGE;
      return;
    }

    let role = "user";
    try {
      const snap = await getDoc(doc(db, "users", user.uid));
      if (snap.exists()) role = snap.data().role || "user";
    } catch (e) {
      console.error("Rol bilgisi okunamadı:", e);
    }
    const isAdmin = role === "admin";

    if (requireAdmin && !isAdmin) {
      // Sıradan üye admin sayfasına giremez, izinli tek sayfaya yönlendirilir.
      window.location.href = MEMBER_ALLOWED_PAGE;
      return;
    }

    onReady(user, isAdmin);
  });
}

export { auth };
