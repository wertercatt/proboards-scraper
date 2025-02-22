import asyncio
import json
import logging
import os
import re
import time
from typing import Tuple

import bs4
import cssutils

from .utils import int_, split_url
from proboards_scraper import ScraperManager


#TODO: 1) QUERY LAST RECORDED POST 2) COUNT UP GET ALL FUTURE POSTS 3) CHECK IF THREAD IN DATABASE BEFORE ADDING POST
#TODO: 1) GET LAST USER ADDED 2) COUNT UP GET ALL FUTURE USERS 

logger = logging.getLogger(__name__)


async def scrape_user(url: str, manager: ScraperManager) -> None:
    """
    Scrape a user profile and add the user to the ScraperManager's user queue
    (from which the user will be inserted into the database), as well as
    download the user's avatar and insert the image into the database.

    Args:
        url: User profile page URL.
        manager: ScraperManager instance.
    """
    # Get user id from URL, eg, "https://xyz.proboards.com/user/42" has
    # user id 42. We can exploit os.path.split() to grab everything right
    # of the last backslash.
    uid=os.path.split(url)[1]
    user = {
        "url": url,
        "id": int(uid)
    }

    check = manager.db.query_check(uid, type="user")

    if check is not None:
        print("User " + uid + " is in database.")
        #return

    source = await manager.get_source(url)
    time.sleep(3)
    user_container = source.find("div", {"class": "show-user"})

    # Get display name and group.
    name_and_group = user_container.find("div", class_="name_and_group float-right")
    user["name"] = name_and_group.find("span", class_="big_username").text

    # The group name is contained between two <br> tags and is the element's
    # fourth child.
    children = [child for child in name_and_group.children]
    user["group"] = children[3].strip()

    # Get username and last online datetime.
    controls = user_container.find("div", class_="float-right controls")
    user_datetime = controls.find("div", class_="float-right clear pad-top")
    children = [child for child in user_datetime.children]
    for i, child in enumerate(children):
        if isinstance(child, bs4.element.NavigableString):
            if child.strip() == "Username:":
                user["username"] = children[i+1].text
            elif child.strip() == "Last Online:":
                # Get Unix timestamp string from <abbr> tag.
                lastonline_block = children[i+1]
                unix_ts = int(lastonline_block.find("abbr")["data-timestamp"])
                user["last_online"] = unix_ts
            elif child.strip() == "Member is Online":
                # This will be the case for the aiohttp session's logged-in
                # user (and for any other user that happens to be logged in).
                # Multiply time.time() value by 1000 for milliseconds.
                unix_ts = int(time.time()) * 1000
                user["last_online"] = unix_ts

    # Get rest of user info from the table in the user status form.
    status_form = user_container.find(
        "div", class_="pad-all-double ui-helper-clearfix clear"
    )
    main_table = status_form.find(id="center-column")

    # Extract "content boxes" (<div> elements containing different classes of
    # info) from the "main table".
    content_boxes = main_table.find_all("div", class_="content-box")

    # The first row ("content box") of the main table is always present and
    # contains another table, where each row contains two columns: the first
    # is a heading specifying the type of info (eg, "Email:"), and the second
    # contains its value.

    # NOTE: for the session's logged-in user, the first row contains a
    # "status update" form input, which we identify and delete if necessary.
    status_input = content_boxes[0].find("td", class_="status-input")
    if status_input:
        content_boxes.pop(0)
    for content_box in content_boxes: #badges take up the first content_box
        for row in content_box.find_all("tr"):

            row_data = row.find_all("td")
            heading = row_data[0].text.strip().rstrip(":")
            val = row_data[1]
            if heading == "Age":
                user["age"] = int(val.text)
            elif heading == "Birthday":
                user["birthdate"] = val.text
            elif heading == "Date Registered":
                user["date_registered"] = int(val.find("abbr")["data-timestamp"])
            elif heading == "Email":
                user["email"] = val.text
            elif heading == "Gender":
                user["gender"] = val.text
            elif heading == "Latest Status":
                user["latest_status"] = val.find("span").text
            elif heading == "Location":
                user["location"] = val.text
            elif heading == "Posts":
                # Remove commas from post count (eg, "1,500" to "1500").
                user["post_count"] = int_(val.text)
            elif heading == "Web Site":
                website_anchor = val.find("a")
                user["website_url"] = website_anchor.get("href")
                user["website"] = website_anchor.text

    # The rest of the relevant content boxes may or not be present.
    for content_box in content_boxes[1:]:
        # We use the first child to determine the content type, ignoring
        # any unnecessary newlines.
        children = [
            child for child in content_box.children if str(child) != "\n"
        ]
        first_child = children[0]

        if (
            isinstance(first_child, bs4.element.NavigableString)
            and first_child.strip() == "Signature"
        ):
            # Grab the entire signature HTML as a string; the signature starts
            # after a horizontal line <hr>, ie, the box's 3rd child and ends
            # before the last element
            signature = "".join([str(child) for child in children[2:]])
            user["signature"] = signature
        elif (
            isinstance(first_child, bs4.element.Tag)
            and "social" in first_child.get("class", [])
            and "messengers" in first_child.get("class", [])
        ):
            # Construct the instant messenger string that will be inserted
            # into the database. Each messenger label has the form
            # "{messenger}:", eg, "AIM:", and the next tag (sibling) is the
            # messenger screen name. The constructed string is of the form
            # "{messenger1}:{screenname1};{messenger2}:{screenname2}:..."
            # where each messenger type is delimited by a semicolon, eg:
            # "AIM:ssj_goku12;ICQ:12345;YIM:duffman20"
            messenger_str_list = []

            messenger_labels = first_child.find_all("span", class_="label")
            for messenger_label in messenger_labels:
                messenger = messenger_label.text
                screen_name = messenger_label.next_sibling.text
                messenger_str_list.append(f"{messenger}{screen_name}")

            messenger_str = ";".join(messenger_str_list)
            user["instant_messengers"] = messenger_str

    await manager.user_queue.put(user)

    # Get avatar image URL. We wait until after adding the user so to ensure
    # that the user is added even if an error is encountered in downloading
    # their avatar.
    avatar_wrapper = user_container.find("div", class_="avatar-wrapper")
    avatar_url = avatar_wrapper.find("img")["src"]

    avatar_ret = await manager.download_image(avatar_url)
    image = avatar_ret["image"]
    image["description"] = "avatar"

    # We need an image id to associate this image with a user as an avatar;
    # thus, we must interact with the database directly to retrieve the
    # image id (if it already exists in the database) or add then retrieve
    # the id of the newly added image (if it doesn't already exist).
    # NOTE: even if the image wasn't obtained successfully or is invalid, we
    # still store an Image in the database that contains the original avatar
    # URL (and an Avatar linking that Image to the current user).

    image_id = manager.insert_image(image)

    avatar = {
        "user_id": user["id"],
        "image_id": image_id,
    }

    manager.db.insert_avatar(avatar)
    logger.debug(f"Got user profile info for user {user['name']}")

    check_ = {
        "id": "user-" + uid,
        "date": time.time_ns()
    }
    manager.db.insert_check(check_)


def scrape_user_urls(source: bs4.BeautifulSoup) -> Tuple[list, str]:
    """
    Grab all user profile links from the given page source (corresponding to
    a page of the members list) and return the user profile links as well as
    the link to the next page of users, if any.

    Args:
        source: Page source for the members page URL, e.g.,
            `https://yoursite.proboards.com/members` or
            `https://yoursite.proboards.com/members?page=2`.

    Returns:
        A list of user profile links on the current page and the link to the
        next page of user profiles, if any.
    """
    # TODO: put this in `scrape_users`.
    member_hrefs = []
    next_href = None

    members_container = source.find("div", class_="container members")
    members_table_rows = members_container.find("tbody").findAll("tr")
    for row in members_table_rows:
        # NOTE: the href attribute is relative and must be appended to the
        # site's base URL to construct the full user URL for a user.
        href = row.find("a")["href"]
        member_hrefs.append(href)

    # Get the URL for the next page button if it's enabled.
    next_ = source.find("li", {"class": "ui-pagination-next"}).find("a")
    if next_.has_attr("href"):
        next_href = next_["href"]

    return member_hrefs, next_href


async def scrape_users(url: str, manager: ScraperManager) -> None:
    """
    Asynchronously iterate over all user profile pages and add them to the
    the ScraperManager user queue for insertion into the database.

    Args:
        url: Main members page URL, e.g.,
            `https://yoursite.proboards.com/members`.
        manager: ScraperManager instance.
    """
    logger.info(f"Getting user profile URLs from {url}")

    base_url, _ = split_url(url)


    next_href = True
    next_url = url

    while next_href:
        member_hrefs = []
        source = await manager.get_source(next_url)
        _member_hrefs, next_url = scrape_user_urls(source)
        member_hrefs.extend(_member_hrefs)

        member_urls = [f"{base_url}{member_href}" for member_href in member_hrefs]
        logger.info(f"Found {len(member_urls)} user profile URLs")

        for member_url in member_urls:
            await scrape_user(member_url, manager)

        '''
        loop = asyncio.get_running_loop()
        tasks = []

        for member_url in member_urls:
            task = loop.create_task(scrape_user(member_url, manager))
            tasks.append(task)

        await asyncio.wait(tasks)
        '''


async def scrape_poll(
    thread_id: int, poll_container: bs4.element.Tag,
    voters_container: bs4.element.Tag, manager: ScraperManager
) -> None:
    """
    Helper function for :func:`scrape_thread` that parses poll HTML and adds
    the poll, poll options, and poll voters and related metadata to the
    ScraperManager content queue for insertion into the database.

    Args:
        thread_id: Thread ID of the thread to which this poll belongs. Since
            any given thread can have, at most, one poll, a thread ID can be
            used to uniquely identify a corresponding poll.
        poll_container: BeautifulSoup HTML container for the poll.
        voters_container: BeautifulSoup HTML container for poll voters.
        manager: ScraperManager instance.
    """
    poll_name = poll_container.find("h3").text.strip()

    poll = {
        "type": "poll",
        "id": thread_id,
        "name": poll_name,
    }
    await manager.content_queue.put(poll)

    poll_results = poll_container.find("table", class_="results")
    poll_options = poll_results.findAll("tr")

    for poll_option_ in poll_options:
        # Each poll option has a sitewide unique id that can be found in
        # the <tr> elements class, e.g., <tr class="answer-123">, where
        # 123 is the poll option (answer) id.
        poll_option_id = int(poll_option_["class"][0].split("-")[1])

        poll_option_answer = poll_option_.find("td", class_="answer")
        poll_option_name = poll_option_answer.find("div").text.strip()

        vote_info = poll_option_.find("td", class_="view-votes")
        votes = int(vote_info.find("span", class_="votes").text)

        poll_option = {
            "type": "poll_option",
            "id": poll_option_id,
            "poll_id": thread_id,
            "name": poll_option_name,
            "votes": votes,
        }
        await manager.content_queue.put(poll_option)

    # Get poll voters.
    poll_voters = voters_container.findAll("div", class_="micro-profile")
    for voter in poll_voters:
        voter_anchor = voter.find("div", class_="info").find("a")
        user_id = int(voter_anchor["data-id"])

        poll_voter = {
            "type": "poll_voter",
            "poll_id": thread_id,
            "user_id": user_id,
        }
        await manager.content_queue.put(poll_voter)


async def scrape_thread(url: str, manager: ScraperManager) -> None:
    """
    Scrape all pages of a thread, including poll (if any) and all posts,
    and add them to the content queue for insertion into the database.

    Args:
        url: Thread URL.
        manager: ScraperManager instance.
    """
    # Get thread id from URL.
    base_url, url_path = split_url(url)
    thread_id = int(url_path.split("/")[2])

    print("thread id: " + str(thread_id))
    check = manager.db.query_check(thread_id, type="thread")

    if check is not None:
        print("Thread is in database.")
        #return

    # Polls are loaded with the aid of JavaScript; if the thread contains
    # a poll, we ust selenium/Chrome to get the source. However, the source
    # obtained through selenium is different from the source obtained without
    # it, so we must also obtain the source via aiohttp as usual to ensure
    # that the rest of the elements can be scraped (e.g., the next page button
    # has a different class when JS is enabled).

    source = await manager.get_source(url)

    # All thread metadata is contained in a particular <script> element in a
    # somewhat convoluted manner. The method for extracting it is equally
    # convoluted but implemented below with as much clarity as possible.
    metadata_script = None

    metadata_expr = '"thread":({.*?})'
    script_tags = source.findAll("script")
    for script_tag in script_tags:
        script = str(script_tag.string)
        if script.startswith("proboards.data("):
            metadata_script = script
            break
    else:
        logger.error(
            f"Failed to find thread metadata script tag for {url}"
        )

    # Use the ``json`` module to get the metadata as a Python dict.
    metadata_match = re.search(metadata_expr, metadata_script)
    metadata_str = metadata_match.groups()[0]
    metadata = json.loads(metadata_str)

    announcement = metadata["is_announcement"] == 1
    board_id = metadata["board_id"]
    locked = metadata["is_locked"] == 1
    poll = metadata["is_poll"] == 1
    sticky = metadata["is_sticky"] == 1
    user_id = metadata["created_by"]
    views = int_(metadata["views"])

    # If the create user id is 0, it means the user who created the thread
    # is a guest. In this case, we jump ahead to the first post to grab the
    # guest user name and get a database user id for the guest.
    if user_id == 0:
        first_post = source.select("tr.post.first")[0]

        guest_user_name = first_post.find("span", class_="user-guest")

        if guest_user_name is None: #weird error if OP is guest and deletes post
            guest_user_name = "user-guest"
        else:
            guest_user_name = guest_user_name.text
        user_id = manager.insert_guest(guest_user_name)

    if poll:
        manager.driver.get(url)
        time.sleep(1)

        # Click the "View Voters" button, which causes a modal to load.
        manager.driver.find_element_by_link_text("View Voters").click()
        time.sleep(1)

        selenium_source = manager.driver.page_source
        selenium_source = bs4.BeautifulSoup(selenium_source, "html.parser")
        selenium_post_container = selenium_source.find(
            "div", class_="container posts"
        )
        poll_container = selenium_post_container.find("div", class_="poll")
        voters_container = selenium_source.find("div", {"id": "poll-voters"})
        await scrape_poll(thread_id, poll_container, voters_container, manager)

    post_container = source.find("div", class_="container posts")
    title_bar = post_container.find("div", class_="title-bar")
    thread_title = title_bar.find("h1").text

    thread = {
        "type": "thread",
        "announcement": announcement,
        "board_id": board_id,
        "id": thread_id,
        "locked": locked,
        "sticky": sticky,
        "title": thread_title,
        "url": url,
        "user_id": user_id,
        "views": views,
    }
    await manager.content_queue.put(thread)

    def parse_likes(like_):
        more = like_.find_elements_by_class_name("view-likes")
        if len(more) > 0:
            users = []
            more[0].click()
            while True:
                attempts = 5
                while attempts > 0:
                    try:
                        time.sleep(0.8)
                        user_dialog = manager.driver.find_elements_by_class_name("users")
                        user_dialog = user_dialog[-1]
                        users_page = [x.get_attribute("data-id") for x in user_dialog.find_elements_by_class_name("user-link")]
                        next_button = user_dialog.find_elements_by_class_name("ui-pagination-next")[0]

                        users.extend(users_page)
                        attempts=0

                    except Exception as e:
                        attempts -= 1
                        if attempts < 0: 
                            raise ValueError("Could not parse like dialog box")

                button_classes = next_button.get_attribute("class").split(" ")
                if "state-disabled" in button_classes: 
                    break

                next_button.click()    
        else:
            user_likes = like_.find_elements_by_class_name("user-link")
            users = [x.get_attribute("href").split("/")[-1] for x in user_likes]

        return users

    pages_remaining = True
    while pages_remaining:
        post_likes = {}

        for like_ in manager.driver.find_elements_by_class_name("likes"):
            element = like_
            while True:
                element = element.find_element_by_xpath("..")
                if element.tag_name == "tr":
                    classes = element.get_attribute("class").split(" ")
                    if "post" in classes:
                        break

            post_id = element.get_attribute("id")
            pid = post_id.split("-")[1]
            users = parse_likes(like_)
            post_likes[pid] = users

        for post_id in post_likes:
            for user_id in post_likes[post_id]:
                like_ = {
                    "type": "like",
                    "id": post_id + "-" + user_id,
                    "post_id": int(post_id),
                    "user_id": int(user_id),
                }
                await manager.content_queue.put(like_)

        posts = post_container.findAll("tr", class_="post")

        for post_ in posts:

            # Each post <tr> tag has an id attribute of the form
            # <tr id="post-1234">, where 1234 is the post id.
            post_id = int(post_["id"].split("-")[1])

            # "left panel" contains info about the user who made the post.
            left_panel = post_.find("td", class_="left-panel")

            deleted_user=False

            if guest_ := left_panel.find("span", class_="user-guest"):
                guest_user_name = guest_.text
                guest_id = manager.insert_guest(guest_user_name)
                user_id = guest_id
            elif(left_panel.find("div", class_="deleted-mini-profile")):
                user_id = -1
                deleted_user=True
            else:
                # <a> tag href attribute is of the form "/user/5".
                user_link = left_panel.find("a", class_="user-link")
                user_id = user_link["href"]
                if user_id == "http://support.proboards.com": # proboards staff
                    user_id = -3
                else:
                    user_id = int(user_id.split("/")[-1])

            post_content = post_.find("td", class_="content")
            post_info = post_content.find("div", class_="info")

            date_abbr = post_info.find("span", class_="date").find("abbr")
            date = int(date_abbr["data-timestamp"])

            article = post_content.find("article")
            message_ = article.find("div", class_="message")
            message = "".join(str(child) for child in message_.children)

            last_edited = None
            edit_user_id = None

            edited_by = post_.find("div", class_="edited_by")
            if edited_by is not None:
                last_edited = int(edited_by.find("abbr")["data-timestamp"])
                edit_user_anchor = edited_by.find("a")

                if edit_user_anchor is None:

                    if deleted_user:
                        edit_user_href="Deleted User"
                        edit_user_id=-1
                    else:
                        # This represents the case where a guest user has edited
                        # their own post.
                        edit_guest = edited_by.find("span", class_="user-guest")

                        if edit_guest is None: #weird error if OP is guest and deletes post
                            guest_user_name = "user-guest"
                        else:
                            guest_user_name = edit_guest.text

                        edit_user_id = manager.insert_guest(guest_user_name)
                else:
                    edit_user_href = edited_by.find("a")["href"]
                    edit_user_id = int(edit_user_href.split("/")[-1])

            post = {
                "type": "post",
                "id": post_id,
                "date": date,
                "edit_user_id": edit_user_id,
                "last_edited": last_edited,
                "message": message,
                "thread_id": thread_id,
                "url": f"{base_url}/post/{post_id}",
                "user_id": user_id,
            }
            await manager.content_queue.put(post)


        # Continue to next page, if any.
        control_bar = post_container.find("div", class_="control-bar")
        next_btn = control_bar.find("li", class_="ui-pagination-next")

        if "state-disabled" in next_btn["class"]:
            pages_remaining = False
        else:
            next_href = next_btn.find("a")["href"]
            next_url = next_href #f"{base_url}{next_href}"
            print("NEXT PAGE:" + next_url)
            source = await manager.get_source(next_url)
            post_container = source.find("div", class_="container posts")


    check_ = {
        "id": "thread-" + str(thread_id),
        "date": time.time_ns()
    }
    manager.db.insert_check(check_)

async def scrape_board(url: str, manager: ScraperManager) -> None:
    """
    Scrape a board, including all sub-boards (recursively) and all threads,
    and add them to the content queue for insertion into the database.

    Args:
        url: Board page URL.
        manager: ScraperManager instance.
    """
    # Board URLs take the form:
    # https://yoursite.proboards.com/board/{id}/{name}
    base_url, url_path = split_url(url)
    board_id = int(url_path.split("/")[2])

    source = await manager.get_source(url)

    # Get board name and description from Information/Statistics container.
    stats_container = source.find("div", class_="container stats")

    # Get category id and parent board id (if any, i.e., if this is a
    # sub-board) from the navigation tree at the top of the page.
    # The first nav-tree item contains no useful information. The second
    # contains a link to the category. The last corresponds to the current
    # board. If there are more than three items, the second-to-last contains
    # the parent board.
    nav_tree = source.find("ul", id="nav-tree").findAll("li")

    # The category <li> tag contains an anchor tag with a href as follows:
    # <a href="/#category-4">
    # where, in this example, the category id is 4.
    category_li = nav_tree[1]
    category_href = category_li.find("a")["href"]
    category_id = int(category_href.split("-")[1])

    # The parent board <li> tag (if any) contains an anchor tag with a href as
    # follows: <a href="/board/12/board-name">
    # where, in this example, the parent board id is 12.
    parent_id = None
    if len(nav_tree) > 3:
        parent_board_li = nav_tree[-2]
        parent_board_href = parent_board_li.find("a")["href"]
        parent_id = int(parent_board_href.split("/")[-2])

    if source.find("a", id="moderators-link") is not None:
        manager.driver.get(url)
        time.sleep(1)
        manager.driver.find_element_by_id("moderators-link").click()
        time.sleep(1)

        selenium_source = bs4.BeautifulSoup(
            manager.driver.page_source, "html.parser"
        )

        micro_profiles = selenium_source.findAll("div", class_="micro-profile")
        for micro_profile in micro_profiles:
            user_id = int(micro_profile.find("a")["data-id"])

            moderator = {
                "type": "moderator",
                "user_id": user_id,
                "board_id": board_id,
            }
            await manager.content_queue.put(moderator)

    description = None
    password_protected = None
    if (
        (not stats_container)
        and ("This board is password protected" in str(source))
    ):
        container = source.find("div", class_="container")
        title_heading = container.find("div", class_="title-bar").find("h2")
        board_name = title_heading.text
        password_protected = True
    else:
        board_name = stats_container.find("div", class_="board-name")
        if board_name is not None:
            board_name = board_name.text
        else: # if no board name is not provided in the stats container
            board_name = source.find("h1").text 

        description = stats_container.find("div", class_="board-description")
        if description is not None:
            description = description.text
        else: # if no board description is not provided in the stats container
            description = ""

    board = {
        "type": "board",
        "category_id": category_id,
        "description": description,
        "id": board_id,
        "name": board_name,
        "parent_id": parent_id,
        "password_protected": password_protected,
        "url": url,
    }
    await manager.content_queue.put(board)

    # Add any sub-boards to the queue.
    subboard_container = source.find("div", class_="container boards")
    if subboard_container:
        subboards = subboard_container.find("tbody").findAll("tr")

        for subboard in subboards:
            clickable = subboard.find("td", class_="main clickable")
            link = clickable.find("span", class_="link").find("a")
            href = link["href"]
            subboard_url = base_url + href

            await scrape_board(subboard_url, manager)

    # Iterate over all board pages and add threads on each page to queue.
    thread_container = source.find("div", class_="container threads")

    if thread_container:
        pages_remaining = True
        while pages_remaining:
            thread_tbody = thread_container.find("tbody")
            threads = thread_tbody.findAll("tr", class_="thread")

            for thread_ in threads:
                clickable = thread_.find("td", class_="main clickable")
                anchor = clickable.find("span", class_="link target").find("a")
                thread_href = anchor["href"]
                thread_url = base_url + thread_href

                await scrape_thread(thread_url, manager)

            # control-bar contains pagination/navigation buttons.
            control_bar = thread_container.find("ul", class_="ui-pagination")
            next_btn = control_bar.find("li", class_="ui-pagination-next")

            if "state-disabled" in next_btn["class"]:
                pages_remaining = False
            else:
                next_page_href = next_btn.find("a")["href"]
                next_page_url = next_page_href
                logger.info(f"Getting source for {next_page_url}")
                source = await manager.get_source(next_page_url)
                thread_container = source.find(
                    "div", class_="container threads"
                )


async def scrape_shoutbox(
    shoutbox_container: bs4.element.Tag, manager: ScraperManager
) -> None:
    """
    Scrape the shoutbox on the home page and add all shoutbox posts to the
    content queue for insertion into the database.

    Args:
        shoutbox_container: BeautifulSoup HTML corresponding to the shoutbox.
        manager: ScraperManager instance.
    """
    shoutbox_posts = shoutbox_container.findAll("div", class_="shoutbox-post")

    post_id_expr = r"shoutbox-post-(\d+)"

    for post in shoutbox_posts:
        post_id = None
        for class_ in post["class"]:
            if match := re.match(post_id_expr, class_):
                post_id = match.groups()[0]

        timestamp = int(post.find("abbr", class_="time")["data-timestamp"])
        message = post.find("span", class_="message").text
        user_id = int(post.find("a", class_="user-link")["data-id"])

        shoutbox_post = {
            "type": "shoutbox_post",
            "id": post_id,
            "date": timestamp,
            "message": message,
            "user_id": user_id,
        }
        await manager.content_queue.put(shoutbox_post)


async def scrape_smileys(
    smiley_menu: bs4.element.Tag, manager: ScraperManager
) -> None:
    """
    Helper function for :func:`scrape_forum` that grabs all smileys available
    in the post editor form, downloading the images and adding them to the
    content queue for insertion into the database. The description for each
    smiley, which is represented as an image in the `Image` table in the
    database, is the word "smiley" followed by the emoticon it represents,
    e.g., `"smiley :)"`.

    Args:
        smiley_menu: BeautifulSoup HTML source corresponding to the smiley
            menu from a post editor form.
        manager: ScraperManager instance.
    """
    for smiley in smiley_menu.findAll("li"):
        img_tag = smiley.find("img")
        emoticon = img_tag["title"]
        img_url = img_tag["src"]

        smiley_ret = await manager.download_image(img_url)

        image = smiley_ret["image"]
        image["type"] = "image"
        image["description"] = f"smiley {emoticon}"
        await manager.content_queue.put(image)


async def scrape_forum(url: str, manager: ScraperManager) -> None:
    """
    Recursively scrape the site beginning at the homepage (main forum page),
    including all categories, boards, smileys, and the shoutbox. These items
    are added to the ScraperManager content queue for insertion into the
    database.

    Args:
        url: Forum homepage URL.
        manager: ScraperManager instance.

    .. note::
        This function does NOT scrape user profiles. User profiles must
        be scraped in a separate ``async`` task via :func:`scrape_users`.
    """
    # Use selenium to get the page source because it will load the smileys
    # in the shoutbox post area.
    manager.driver.get(url)
    time.sleep(1)
    source = bs4.BeautifulSoup(manager.driver.page_source, "html.parser")

    # Grab favicon.
    favicon_url = source.find("link", {"rel": "icon"})["href"]
    favicon_ret = await manager.download_image(favicon_url)
    favicon_image = favicon_ret["image"]
    favicon_image["description"] = "favicon"
    favicon_image["type"] = "image"
    await manager.content_queue.put(favicon_image)

    # Grab site background and banner from CSS (TODO: add CSS to database?)
    bg_image_expr = r"url\((.+)\)"
    stylesheets = source.findAll("link", {"rel": "stylesheet"})
    for i, stylesheet in enumerate(stylesheets):
        css_href = stylesheet["href"]
        css_url = f"https:{css_href}"
        css = await manager.get_source(css_url)

        sheet = cssutils.parseString(css.text)
        for rule in sheet:
            if (
                rule.type == rule.STYLE_RULE
                and "background-image" in rule.style
                and rule.selectorText in ("body", "#banner")
            ):
                # Background image CSS styles have the form
                # 'url(http://domain.com/path/to/img.jpg)'.
                bg_image_style = rule.style["background-image"]
                bg_image_match = re.match(bg_image_expr, bg_image_style)
                bg_image_url = bg_image_match.groups()[0]

                bg_image_ret = await manager.download_image(bg_image_url)
                bg_image = bg_image_ret["image"]
                bg_image["type"] = "image"

                if rule.selectorText == "body":
                    bg_image["description"] = "background"
                elif rule.selectorText == "#banner":
                    bg_image["description"] = "banner"

                await manager.content_queue.put(bg_image)

    smiley_menu = source.find("ul", class_="smiley-menu")
    await scrape_smileys(smiley_menu, manager)

    shoutbox_container = source.find("div", class_="shoutbox_container")
    await scrape_shoutbox(shoutbox_container, manager)

    categories = source.findAll("div", class_="container boards")
    for category_ in categories:
        # The category id is found among the tag's previous siblings and looks
        # like <a name="category-2"></a>. We want the number in the name attr.
        category_id_tag = list(category_.previous_siblings)[1]
        category_id = int(category_id_tag["name"].split("-")[1])

        title_bar = category_.find("div", class_="title_wrapper")
        category_name = title_bar.text

        # Add category to database queue.
        category = {
            "type": "category",
            "id": category_id,
            "name": category_name,
        }
        await manager.content_queue.put(category)

        boards = category_.findAll(
            "tr",
            {"class": ["o-board", "board", "item"]}
        )

        for board_ in boards:
            clickable = board_.find("td", class_="main clickable")
            link = clickable.find("span", class_="link").find("a")
            href = link["href"]
            board_url = f"{url}{href}"
            await scrape_board(board_url, manager)
