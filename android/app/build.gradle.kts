plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.android)
    alias(libs.plugins.ksp)
}

android {
    namespace = "lk.ac.nibm.gnlc"
    compileSdk = 35

    defaultConfig {
        applicationId = "lk.ac.nibm.gnlc"
        // 26 covers the Galaxy A21s the evaluation runs on and everything
        // newer. Going lower would pull in CameraX compatibility work that
        // buys nothing, since no device on the floor is older.
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
    }

    buildTypes {
        release {
            // Left off deliberately. A shrunk release build strips the Room
            // and Retrofit reflection this app relies on unless the keep
            // rules are exactly right, and debugging that during an
            // evaluation window is not a good use of the time.
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        // 17, not 21. JDK 21 compiles this fine; targeting 21 bytecode is
        // what the Android toolchain objects to.
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
        // AGP 8.0 stopped generating BuildConfig by default, so this has to
        // be asked for. ApiProvider uses BuildConfig.DEBUG to decide whether
        // to log HTTP traffic — verbose logging is wanted while testing on
        // the bench and not on the packing floor.
        buildConfig = true
    }

    sourceSets {
        getByName("androidTest").assets.srcDir("$projectDir/schemas")
    }

    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
    }
}

/**
 * Where Room writes the exported schema.
 *
 * `exportSchema = true` on the @Database without this setting is a build
 * warning and produces nothing. With it, `app/schemas/…/1.json` is written on
 * every build: a machine-readable record of the device-side schema, which is
 * worth having in the repository alongside `db/schema.sql` for Chapter 4, and
 * which is what a future migration would be written against.
 */
ksp {
    arg("room.schemaLocation", "$projectDir/schemas")
}

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.appcompat)
    implementation(libs.material)
    implementation(libs.androidx.constraintlayout)
    implementation(libs.androidx.activity.ktx)
    implementation(libs.androidx.preference.ktx)
    implementation(libs.androidx.recyclerview)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.androidx.lifecycle.viewmodel.ktx)

    implementation(libs.androidx.camera.core)
    implementation(libs.androidx.camera.camera2)
    implementation(libs.androidx.camera.lifecycle)
    implementation(libs.androidx.camera.view)

    implementation(libs.mlkit.barcode)
    implementation(libs.androidx.exifinterface)
    implementation(libs.kotlinx.coroutines.android)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)

    implementation(libs.androidx.work.runtime.ktx)

    implementation(libs.retrofit)
    implementation(libs.retrofit.gson)
    implementation(libs.okhttp)
    implementation(libs.okhttp.logging)
    implementation(libs.gson)

    testImplementation(libs.junit)
    androidTestImplementation(libs.androidx.junit)
    androidTestImplementation(libs.androidx.espresso.core)
}
