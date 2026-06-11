--------------------------------------------------------------------------------
{-# LANGUAGE OverloadedStrings #-}
import Control.Applicative ((<$>))
import Data.Monoid (mappend)
import Hakyll
import Hakyll.Core.Identifier.Pattern ((.||.))
import qualified GHC.IO.Encoding as E

config :: Configuration
config = defaultConfiguration

myFeedConfiguration :: FeedConfiguration
myFeedConfiguration = FeedConfiguration
    { feedTitle       = "~agentydragon"
    , feedDescription = "*agentydragon noises*"
    , feedAuthorName  = "Rai"
    , feedAuthorEmail = "agentydragon@gmail.com"
    , feedRoot        = "http://agentydragon.com"
    }

type FeedRenderer =
    FeedConfiguration
    -> Context String
    -> [Item String]
    -> Compiler (Item String)
feedCompiler :: FeedRenderer -> Compiler (Item String)
feedCompiler renderer =
    renderer myFeedConfiguration feedCtx
        =<< fmap (take 10) . recentFirst
        =<< loadAllSnapshots "posts/*" "content"

--------------------------------------------------------------------------------
main :: IO ()
main = do
  E.setLocaleEncoding E.utf8
  hakyllWith config $ do
    match ("images/*" .||. "static/*") $ do
      route idRoute
      compile copyFileCompiler

    match "css/*" $ do
      route idRoute
      compile compressCssCompiler

    match "CNAME" $ do
      route idRoute
      compile copyFileCompiler

    match ("about.markdown" .||. "found.markdown" .||. "nfc.markdown") $ do
      route $ setExtension "html"
      compile $ pandocCompiler
        >>= loadAndApplyTemplate "templates/default.html" defaultContext
        >>= relativizeUrls

    match "posts/*" $ do
      route $ setExtension "html"
      compile $ pandocCompiler
        >>= saveSnapshot "content"
        >>= loadAndApplyTemplate "templates/post.html" postCtx
        >>= loadAndApplyTemplate "templates/default.html" postCtx
        >>= relativizeUrls

    create ["archive.html"] $ do
      route idRoute
      compile $ do
        posts <- recentFirst =<< loadAll "posts/*"
        let
          archiveCtx = listField "posts" postCtx (return posts) `mappend`
                       constField "title" "Archive" `mappend`
                       defaultContext

        makeItem ""
          >>= loadAndApplyTemplate "templates/archive.html" archiveCtx
          >>= loadAndApplyTemplate "templates/default.html" archiveCtx
          >>= relativizeUrls

    create ["atom.xml"] $ do
      route idRoute
      compile (feedCompiler renderAtom)

    create ["rss.xml"] $ do
      route idRoute
      compile (feedCompiler renderRss)

    match "index.html" $ do
      route idRoute
      compile $ do
        posts <- ((take 10 <$>) . recentFirst) =<< loadAllSnapshots "posts/*" "content"
        let
          indexCtx = listField "posts" postCtx (return posts) `mappend`
                     constField "title" "Home" `mappend`
                     defaultContext

        getResourceBody >>= applyAsTemplate indexCtx
          >>= loadAndApplyTemplate "templates/default.html" indexCtx
          >>= relativizeUrls

    match "templates/*" $ compile templateCompiler

--------------------------------------------------------------------------------
postCtx :: Context String
postCtx = dateField "date" "%Y-%m-%d" `mappend` defaultContext

feedCtx = postCtx `mappend` bodyField "description"
