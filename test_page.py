
from storage.page import SlottedPage
from storage.disk_manager import DiskManager


page = SlottedPage(page_id=1)

print("Page created successfully")
print("Initial free space:", page.get_free_space())


slot1 = page.insert_record(b"Customer: Ahmed")
slot2 = page.insert_record(b"Customer: Sara")
slot3 = page.insert_record(b"Customer: Mohamed")

print("\nInserted records:")
print("Slot 1:", page.get_record(slot1))
print("Slot 2:", page.get_record(slot2))
print("Slot 3:", page.get_record(slot3))


page.delete_record(slot2)

print("\nDeleted slot:", slot2)


new_slot = page.insert_record(b"Customer: Ali")

print("Reused slot:", new_slot)
print("New record:", page.get_record(new_slot))


page.compact()

print("\nCompaction completed")
print("Free space after compaction:", page.get_free_space())


print("Page valid:", page.validate())


serialized_page = page.to_bytes()

restored_page = SlottedPage.from_bytes(serialized_page)

print("\nSerialized page size:", len(serialized_page))
print("Restored page ID:", restored_page.page_id)
print("Restored record:", restored_page.get_record(slot1))


db = DiskManager("page_test.db")

page_id = db.allocate_page()

page.page_id = page_id
db.write_page(page_id, page.to_bytes())

saved_data = db.read_page(page_id)

loaded_page = SlottedPage.from_bytes(saved_data)

print("\nDisk persistence test:")
print("Loaded page ID:", loaded_page.page_id)
print("Loaded record:", loaded_page.get_record(slot1))

db.close()

print("\nAll tests completed successfully!")
