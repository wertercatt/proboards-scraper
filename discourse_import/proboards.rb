# frozen_string_literal: true

require 'sqlite3'
require 'reverse_markdown'
require File.expand_path(File.dirname(__FILE__) + "/base.rb")

class ImportScripts::ProBoards < ImportScripts::Base

  BATCH_SIZE = 10
  
  #PROBOARDS_DB = "/shared/site/forum.db"
  #AVATAR_DIR = "/shared/site/images/"
  
  PROBOARDS_DB = "/home/scott/projects/e4/scraper/site/forum.db"
  AVATAR_DIR = "/home/scott/projects/e4/scraper/site/images/"
  
  def initialize
    super
    @client = SQLite3::Database.open PROBOARDS_DB
  end

  def execute
      
    SiteSetting.tagging_enabled = true

    #import_users
    #import_avatars
    #import_categories
    #import_topics
    import_posts
    #import_likes
    
    #import_groups
    #import_group_users

  end

  def import_users
    puts "------", "creating users"

    @last_user_id = -1
    total_count = @client.get_first_value("SELECT count(*) count FROM user")
    puts "USER COUNT:"
    puts total_count
    
    batches(BATCH_SIZE) do |offset|
      users = @client.execute(
        "SELECT id, email, name, date_registered, last_online,
         latest_status, location, birthdate, post_count                
         FROM user
         WHERE id > #{@last_user_id}
         ORDER BY id ASC
         LIMIT #{BATCH_SIZE};")
    
      #puts users.columns
      #break if users.size < 1
      
      break if users.size < 1
      @last_user_id = users[-1][0]

      #users.each do |user| 
      #    puts user[2]
      #    puts Time.zone.at(user[3]/1000)
      #    puts user[3]
      #end
      
      next if all_records_exist? :users, users.map { |u| u[0].to_i }

      create_users(users, total: total_count, offset: offset) do |user|
        #puts "", user
        if user[0] == 2
           user[1] = "scottpratte@yahoo.com"
        end
        
        next if @lookup.user_id_from_imported_user_id(user[0])
        
        #ip_addr, approved, registration_ip_address, post_create_action
        #todo: if banned (check vanilla)?
        { 
          id: user[0],
          email: user[1],
          username: user[2],
          name: user[2],
          created_at: user[3] == nil ? 0 : Time.zone.at(user[3]/1000),
          last_seen_at: user[4] == nil ? 0 : Time.zone.at(user[4]/1000),
          bio_raw: user[5],
          location: user[6],
          date_of_birth: user[7],
          admin: false,
          trust_level: 1
         }
      end
    end
  end
  

  def import_avatars

    puts "------", "importing user avatars"

    User.find_each do |u|
    
      next unless u.custom_fields["import_id"]
      user_id = u.custom_fields["import_id"]

      image_id = @client.get_first_value("SELECT image_id FROM avatar WHERE user_id = #{user_id};")
      avatar_file = @client.get_first_value("SELECT filename FROM image WHERE id = #{image_id};")

      next if avatar_file == nil
      avatar_path = "#{AVATAR_DIR}#{avatar_file}"

      if !File.exist?(avatar_path)
        puts "Path to avatar file not found! Skipping. #{avatar_path}"
        next
      end
      
      upload = create_upload(u.id, avatar_path, File.basename(avatar_path))
      if upload.persisted?
        u.import_mode = false
        u.create_user_avatar
        u.import_mode = true
        u.user_avatar.update(custom_upload_id: upload.id)
        u.update(uploaded_avatar_id: upload.id)
      else
        puts "Error: Upload did not persist for #{u.username} #{avatar_path}!"
      end
    end
  end


  def import_categories
    puts "------", "importing categories..."

    boards = @client.execute(
       "SELECT id, category_id, name, description
        FROM board
        WHERE id > 0
        ORDER BY id ASC" 
     )

    #top_level_categories = boards.select { |c| c[1].blank? || c[1] == -1 }

    #create_categories(top_level_categories) do |category_id|  
    #  category = @client.get_first_value("SELECT id, name FROM category WHERE id = #{category_id}")
    #  {      
    #    id: category[0],
    #    name: category[1]
    #  }
    #end

    create_categories(boards) do |board|
    
      puts board
      {
        id: board[0],
        name: board[2],
        description: board[3],
      }
    end
  end

  def staff_guardian
    @_staff_guardian ||= Guardian.new(Discourse.system_user)
  end
  
  def clean_up(text)
  
    #puts "", "BEFORE:", text
    
    text = text.gsub(/\<a class="user-link(.*?)title="(.*?)"(.*?)\<span itemprop="name"\>(.*?)\<\/span\>\<\/a\>\<\/span\>/im) { "#{$2}" }
    #text = text.gsub(/\<div author="@(.*?)" class="quote"(.*?)said:\<\/div\>(.*?)\<div class="quote_clear"\>\<\/div\>\<\/div\>\<\/div\>/im) { "\n[quote=\"#{$1}\"]\<br\/\>#{$3}\<br\/\>[\/quote]" }
    
    text = text.gsub(/\<div author="@(.*?)" class="quote"(.*?)said:\<\/div\>/im) { "\n[quote=\"#{$1}\"]\n\<br\/\>" }
    text = text.gsub(/\<div class="quote_clear"\>\<\/div\>\<\/div\>\<\/div\>/im) { "\n\<br\/\>[\/quote]\n\<br\/\>" }
    
    #puts "", "MIDDLE:", text
    
    text = text.gsub(/\<div class="Quote"\>(.*?)\<\/div\>/im) { "\n[quote]\n#{$1}\n[/quote]\n" }
    text = ReverseMarkdown.convert text
    
    #puts "", "AFTER:", text    
    #option = gets

  end




  def import_topics
    puts "------", "importing topics..."

    total_count = @client.get_first_value("SELECT count(*) count FROM thread;")

    @last_thread_id = -1

    batches(BATCH_SIZE) do |offset|
      threads = @client.execute(
        "SELECT id, user_id, title, board_id, views, locked, announcement
         FROM thread
         WHERE id > #{@last_thread_id}
         ORDER BY id ASC
         LIMIT #{BATCH_SIZE};")
      #Body, Format, DateInserted, DateLastComment
      
      break if threads.size < 1
      @last_thread_id = threads[-1][0]
      
      #next if all_records_exist? :posts, threads.map { |t| "thread#" + t[0].to_s }
      #id: "thread#" + threads[0].to_s,
      
      next if all_records_exist? :posts, threads.map { |t| "thread#" + t[0].to_s }
      
      create_posts(threads, total: total_count, offset: offset) do |thread|
      
        posts = @client.execute(
           "SELECT id, thread_id, user_id, message, date
            FROM post
            WHERE thread_id = #{thread[0]}
            ORDER BY id ASC")
        op = posts[0]
      
        puts "", op
        user_id = user_id_from_imported_user_id(thread[1]) || Discourse::SYSTEM_USER_ID
	#todo: check category id      
        #category_id = category_id_from_imported_category_id(thread[3]) || @category_mappings[thread[3]].try(:[], :category_id),
        {
          id: "thread#" + thread[0].to_s,
          user_id: user_id,
          title: thread[2],
          category: thread[3],
          views: thread[4] || 0,
          closed: thread[5] == 0,
          pinned_globally: thread[7] == 1,
          created_at: Time.zone.at(op[4]/1000),
          raw: op[3],
          post_create_action: proc do |post|
             DiscourseTagging.tag_topic_by_names(post.topic, staff_guardian, "Archive")
          end
        }
      end
    end
  end
  
  
  def import_posts
    puts "------", "importing posts..."

    total_count = @client.get_first_value("SELECT count(*) count FROM post;")

    @last_post_id = -1
    
    batches(BATCH_SIZE) do |offset|
      posts = @client.execute(
        "SELECT id, thread_id, user_id, message, date
         FROM post
         WHERE id > #{@last_post_id}
         ORDER BY id ASC
         LIMIT #{BATCH_SIZE};")

      break if posts.size < 1
      @last_post_id = posts[-1][0]
      
      next if all_records_exist? :posts, posts.map { |post| post[0] }

      create_posts(posts, total: total_count, offset: offset) do |post|

        clean_up(post[3])

        
        # There is no "topic post" on proboards so we add the text from the OP 
        # in import_topics and skip the post here
        op_id = @client.get_first_value("SELECT id FROM post WHERE thread_id = #{post[1]} ORDER BY id ASC")       
        next if post[0] == op_id
                
        thread_id = "thread#" + post[1].to_s
        next unless thread = topic_lookup_from_imported_post_id(thread_id)
        next if post[3].blank?
        user_id = user_id_from_imported_user_id(post[2]) || Discourse::SYSTEM_USER_ID
        post = {
          id: post[0],
          user_id: user_id,
          topic_id: thread[:topic_id],
          raw: post[3],
          created_at: Time.zone.at(post[4]/1000)
        }
      end
    end
  end

  
  
end 

ImportScripts::ProBoards.new.perform
