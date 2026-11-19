
from storage.disk_manager import DiskManager


db = DiskManager("my_database.db")


page_id = db.allocate_page()
print("Page ID:", page_id)


db.write_page(page_id, b"Hello Database!")


data = db.read_page(page_id)
print("Page data:", data[:15])


print("Number of pages:", db.get_page_count())


db.close()


db = DiskManager("my_database.db")


data = db.read_page(page_id)
print("Data after reopening:", data[:15])

db.close()
