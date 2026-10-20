package lk.ac.nibm.gnlc.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.TypeConverters

@Database(
    entities = [
        ScanEntity::class,
        ManualEntryEntity::class,
        ChecklistCacheEntity::class,
        BarcodeCacheEntity::class,
        SizeMapCacheEntity::class,
        ExpectationCacheEntity::class,
    ],
    version = 1,
    exportSchema = true,
)
@TypeConverters(Converters::class)
abstract class Db : RoomDatabase() {

    abstract fun scans(): ScanDao
    abstract fun manualEntries(): ManualEntryDao
    abstract fun cache(): CacheDao

    companion object {
        @Volatile private var instance: Db? = null

        fun get(context: Context): Db =
            instance ?: synchronized(this) {
                instance ?: Room.databaseBuilder(
                    context.applicationContext, Db::class.java, "gnlc.db",
                )
                    // No fallbackToDestructiveMigration. This database holds
                    // scans that have not reached the host yet, and wiping it
                    // on a schema change would destroy evidence collected
                    // during an evaluation episode. A future version migrates
                    // or it does not ship.
                    .build()
                    .also { instance = it }
            }
    }
}
