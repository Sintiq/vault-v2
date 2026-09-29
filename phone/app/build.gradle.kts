// Vault — the phone client.
//
// One app, two jobs. Today it is the owner's window into the vault on the PC
// over Tailscale. Later the same app holds the device door the agent knocks
// on, which is why the passport rules belong here and not on the PC: the
// guard stands at the door, not next to whoever is ringing.

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
}

// The release public key is public; the private key and the APK keystore never enter the
// repository. Without phone/release-key.pub the build simply does not check for updates.
val releaseKey: String = rootProject.file("release-key.pub").takeIf { it.isFile }?.readText()?.trim() ?: ""

android {
    namespace = "com.vault.phone"
    compileSdk = 34
    buildToolsVersion = "36.0.0"   // what is already installed on this machine

    defaultConfig {
        applicationId = "com.vault.phone"
        minSdk = 29
        targetSdk = 34
        // versionCode only grows: Android refuses to install an older one over a newer one.
        versionCode = (project.findProperty("vaultVersionCode") as String?)?.toInt() ?: 2
        versionName = (project.findProperty("vaultVersionName") as String?) ?: "0.2"
        buildConfigField("String", "RELEASE_KEY", "\"$releaseKey\"")
    }

    signingConfigs {
        // The owner's release keystore, found through the environment only.
        create("release") {
            System.getenv("VAULT_KEYSTORE")?.let { storeFile = file(it) }
            storePassword = System.getenv("VAULT_KEYSTORE_PASSWORD")
            keyAlias = System.getenv("VAULT_KEY_ALIAS") ?: "vault"
            keyPassword = System.getenv("VAULT_KEY_PASSWORD") ?: System.getenv("VAULT_KEYSTORE_PASSWORD")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (System.getenv("VAULT_KEYSTORE") != null) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true; buildConfig = true }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}

dependencies {
    implementation(platform(libs.compose.bom))
    implementation(libs.compose.ui)
    implementation(libs.compose.ui.graphics)
    implementation(libs.compose.material3)
    implementation(libs.compose.material.icons)
    implementation(libs.activity.compose)
    implementation(libs.lifecycle.runtime.compose)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.okhttp)
    implementation(libs.nanohttpd)
    implementation(libs.health.connect)

    testImplementation(libs.junit)
    testImplementation(libs.json)
}
